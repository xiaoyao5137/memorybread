//! Durable preparation of whole stage trees. Answers only traverse saved nodes.
#[path = "brainstorm_root_axes.rs"]
mod root_axes;
use super::brainstorm_catalog::{self, TrustedCatalog};
use super::*;
use crate::storage::repo::{creation_brainstorm as repo, creation_history, creation_operation};
use root_axes::RootAxesBinding;
use std::collections::{BTreeMap, HashSet};

type Failure = (StatusCode, Json<serde_json::Value>);

// Version 17 prepares content-specific questions for newly started v2 sessions.
// Existing sessions retain their saved v16 preparation contract and questions.
// Version 16 renders atomic candidate copy from finite installed value capabilities.
// Version 15 prepares one terminal question per program-bound atomic property.
// Version 14 compiles topic ownership and roots from one stage attribute selection.
// Version 13 compiles free direction/detail roots from program-owned attributes.
// Version 12 enforces complete Skill policy in every generated stage.
// Version 11 binds candidate factual authority to the current request.
// Version 10 compiles writing child goals and enforces chapter-local scope.
// Version 9 binds writing preparation to unconfirmed decision axes.
// Version 8 adds durable, bounded step recovery.
// Version 7 binds configured skill catalogs to deterministic candidate trees.
// Version 6 also rejects parent goals repeated as child goals.
// Version 5 required decision-space novelty and complete option relationship checks.
// Version 4 required complete semantic checker coverage of every candidate unit.
// Version 3 added independent semantic consistency gates for topic plans/nodes.
// Version 2 introduced scoped goals and program-bound ancestor prompt prefixes.
// Bump when the preparation contract changes accepted goal/scope semantics.
// Unanswered legacy pools are reviewed; unfinished checkpoints are replanned.
const PREPARATION_CONTRACT_VERSION: u32 = 16;
const CONTENT_PREPARATION_CONTRACT_VERSION: u32 = 17;
const MAX_REVIEW_QUESTIONS: usize = 12;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StageWorkflow {
    pub version: u8,
    pub active_stage: String,
    pub stages: Vec<Stage>,
    #[serde(default)]
    pub chapters: Vec<Chapter>,
    pub active_chapter_id: Option<String>,
    #[serde(default)]
    pub copy_repair_history: Vec<CandidateCopyRepair>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    input_return_stage: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CandidateCopyField {
    Label,
    WhyNow,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CandidateCopyPatch {
    pub question_id: String,
    #[serde(default)]
    pub option_id: Option<String>,
    pub field: CandidateCopyField,
    pub expected_text: String,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CandidateCopyRepair {
    pub revision: i64,
    pub question_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub option_id: Option<String>,
    pub field: CandidateCopyField,
    pub old_text: String,
    pub new_text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Stage {
    #[serde(default)]
    pub preparation_contract_version: u32,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    content_mode: bool,
    pub id: String,
    pub title: String,
    pub status: String,
    pub generation_id: String,
    pub questions: Vec<BrainstormQuestion>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub input_preflight: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    input_phase: String,
    #[serde(default)]
    input_attempt: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub(super) confirmed_period: Option<serde_json::Value>,
    pub progress: Progress,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<serde_json::Value>,
    // The manifest is persisted with the tree so restart resumes unpaid batches only.
    #[serde(default)]
    pub topics: Vec<Topic>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub topic_checkpoint: Option<TopicCheckpoint>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub topic_checkpoint_archives: Vec<TopicCheckpointArchive>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub review_checkpoint: Option<PreparationReviewCheckpoint>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub preparation_archives: Vec<PreparationArchive>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    catalog_binding: Option<TrustedCatalog>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    root_axes_binding: Option<RootAxesBinding>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub preparation_progress: Option<PreparationProgress>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recovery_progress: Option<RecoveryProgress>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    automatic_recovery: Option<AutomaticRecovery>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    recovery_history: Vec<RecoveryAudit>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    content_proofs: Vec<ContentQuestionProof>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    content_no_new_decision_reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    content_audit_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    content_archives: Vec<ContentPreparationArchive>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    content_brief_edit_archives: Vec<ContentBriefEditArchive>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    content_root_request_archives: Vec<ContentRootRequestArchive>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ContentQuestionProof {
    question_id: String,
    anchor_source: String,
    anchor_quote: String,
    document_effect: String,
    option_effects: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ContentPreparationArchive {
    reason: String,
    archived_at_ms: i64,
    questions: Vec<BrainstormQuestion>,
    content_proofs: Vec<ContentQuestionProof>,
    content_no_new_decision_reason: Option<String>,
    content_audit_sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ContentBriefEditArchive {
    field_id: String,
    text: String,
    saved_revision: Option<i64>,
    reason: String,
    archived_at_ms: i64,
    sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct ContentRootRequestArchive {
    previous_root_request: String,
    previous_root_input: Option<String>,
    previous_input_revision: Option<i64>,
    new_root_input: String,
    new_input_revision: i64,
    archived_at_ms: i64,
    sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RecoveryProgress {
    pub attempt: usize,
    pub max_attempts: usize,
    pub retry_at_ms: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct AutomaticRecovery {
    #[serde(default)]
    authority_fingerprint: String,
    step_key: String,
    requests_started: usize,
    stage_retries_used: usize,
    // Service outages are recorded as dispatches, but do not spend the
    // candidate-quality retry budget. Both counts survive process restarts.
    #[serde(default)]
    unavailable_failures: usize,
    #[serde(default)]
    unavailable_retries_used: usize,
    max_step_retries: usize,
    max_stage_retries: usize,
    retry_at_ms: i64,
    request_in_flight: bool,
    last_failure_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct RecoveryAudit {
    step_key: String,
    code: String,
    http_status: u16,
    failed_at_ms: i64,
    requests_started: usize,
    stage_retries_used: usize,
    // New consecutive service failures share one bounded audit entry. Old
    // entries have no count and remain immutable when a session is resumed.
    #[serde(default, skip_serializing_if = "is_zero")]
    aggregate_count: usize,
}

fn is_zero(value: &usize) -> bool {
    *value == 0
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PreparationProgress {
    pub topic_title: String,
    pub saved_nodes: usize,
    pub pending_nodes: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TopicCheckpoint {
    #[serde(default)]
    preparation_contract_version: u32,
    topic_id: String,
    topic: Topic,
    build_id: String,
    input_fingerprint: String,
    questions: Vec<BrainstormQuestion>,
    frontier: Vec<NodeGoal>,
    next_node_index: usize,
    #[serde(default)]
    memory_brief: String,
    limits: BuildLimits,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    catalog_binding: Option<TrustedCatalog>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    root_axes_binding: Option<RootAxesBinding>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    catalog_stage: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    root_axis_ids: Vec<String>,
    // Program-derived from exact registered atomic roots; never provider-owned.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    atomic_root_questions: bool,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    root_value_selections: Vec<RootValueCheckpoint>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RootValueCheckpoint {
    node_id: String,
    selection: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TopicCheckpointArchive {
    reason: String,
    archived_at_ms: i64,
    checkpoint: TopicCheckpoint,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PreparationReviewCheckpoint {
    preparation_contract_version: u32,
    input_fingerprint: String,
    pool_fingerprint: String,
    next_index: usize,
    // Freeze the request boundary across a restart or environment change.
    #[serde(default = "default_review_unit_budget")]
    unit_budget: usize,
    #[serde(default)]
    original_status: String,
    #[serde(default)]
    original_generation_id: String,
    #[serde(default)]
    original_progress: Option<Progress>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PreparationArchive {
    reason: String,
    archived_at_ms: i64,
    preparation_contract_version: u32,
    generation_id: String,
    status: String,
    questions: Vec<BrainstormQuestion>,
    topics: Vec<Topic>,
    progress: Progress,
    codes: Vec<String>,
    decision_ids: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    catalog_binding: Option<TrustedCatalog>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    root_axes_binding: Option<RootAxesBinding>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    chapters: Vec<Chapter>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    error: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    automatic_recovery: Option<AutomaticRecovery>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReviewResult {
    review: String,
    codes: Vec<String>,
    decision_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct NodeGoal {
    id: String,
    goal: String,
    depth: usize,
    parent_question_id: Option<String>,
    parent_option_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct BuildLimits {
    max_questions_per_topic: usize,
    max_depth: usize,
}

impl BuildLimits {
    fn configured() -> Self {
        let limits = stage_limits();
        Self {
            max_questions_per_topic: limits["max_questions_per_topic"].as_u64().unwrap() as usize,
            max_depth: limits["max_depth"].as_u64().unwrap() as usize,
        }
    }
    fn minimum_nodes(&self, depth: usize) -> usize {
        if !(2..=3).contains(&self.max_depth)
            || !(3..=24).contains(&self.max_questions_per_topic)
            || depth == 0
            || depth > self.max_depth
        {
            return usize::MAX;
        }
        (1usize << (self.max_depth - depth + 1)) - 1
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TopicPlanResult {
    root_goals: Vec<String>,
}

#[derive(Debug, Serialize, Deserialize)]
struct NextGoals {
    option_id: String,
    goals: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct NodeResult {
    question: BrainstormQuestion,
    next_goals: Vec<NextGoals>,
    #[serde(default)]
    memory_brief: String,
    #[serde(default)]
    root_value_selection: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Progress {
    pub completed_batches: usize,
    pub total_batches: usize,
    pub percent: usize,
    pub started_at_ms: i64,
    pub updated_at_ms: i64,
    pub estimated_remaining_seconds: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Topic {
    pub id: String,
    pub title: String,
    pub goal: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub chapter_id: Option<String>,
    // Internal program-owned topic scope, removed from user responses.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub root_axis_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Chapter {
    pub id: String,
    pub title: String,
    pub goal: String,
    pub status: String,
    pub question_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub instruction_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub operation_id: Option<String>,
}

#[derive(Debug, Deserialize)]
struct PlannedChapter {
    id: String,
    title: String,
    goal: String,
}

#[derive(Debug, Deserialize)]
struct Plan {
    topics: Vec<Topic>,
    #[serde(default)]
    chapters: Vec<PlannedChapter>,
    #[serde(default)]
    memory_brief: String,
    #[serde(default)]
    open_flags: Vec<String>,
}

fn now() -> i64 {
    chrono::Utc::now().timestamp_millis()
}
fn invalid(message: &str) -> Failure {
    brainstorm_error(StatusCode::CONFLICT, "BRAINSTORM_STAGE_CONFLICT", message)
}
fn config(name: &str, default: usize, maximum: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(default)
        .clamp(1, maximum)
}
fn stage_batch_timeout_seconds(configured: Option<&str>) -> u64 {
    configured
        .map(str::trim)
        .filter(|value| value.bytes().all(|byte| byte.is_ascii_digit()))
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|seconds| (30..=600).contains(seconds))
        .unwrap_or(210)
}

fn stage_batch_http_timeout(configured: Option<&str>) -> Duration {
    // Sidecar owns the batch's queue and generation deadline. Allow its safe
    // timeout response to reach Core before the loopback transport expires.
    Duration::from_secs(stage_batch_timeout_seconds(configured) + 30)
}

fn default_review_unit_budget() -> usize {
    8
}

fn review_unit_budget(configured: Option<&str>) -> usize {
    configured
        .map(str::trim)
        .filter(|value| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()))
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|budget| (8..=96).contains(budget))
        .unwrap_or_else(default_review_unit_budget)
}

fn recovery_limit(configured: Option<&str>, default: usize, maximum: usize) -> usize {
    configured
        .map(str::trim)
        .filter(|value| !value.is_empty() && value.bytes().all(|byte| byte.is_ascii_digit()))
        .and_then(|value| value.parse::<usize>().ok())
        .filter(|value| *value <= maximum)
        .unwrap_or(default)
}

impl AutomaticRecovery {
    fn configured() -> Self {
        Self {
            authority_fingerprint: String::new(),
            step_key: String::new(),
            requests_started: 0,
            stage_retries_used: 0,
            unavailable_failures: 0,
            unavailable_retries_used: 0,
            max_step_retries: recovery_limit(
                std::env::var("MEMORYBREAD_BRAINSTORM_STAGE_STEP_RECOVERY_LIMIT")
                    .ok()
                    .as_deref(),
                2,
                5,
            ),
            max_stage_retries: recovery_limit(
                std::env::var("MEMORYBREAD_BRAINSTORM_STAGE_RECOVERY_LIMIT")
                    .ok()
                    .as_deref(),
                8,
                32,
            ),
            retry_at_ms: 0,
            request_in_flight: false,
            last_failure_code: None,
        }
    }
    fn enabled(&self) -> bool {
        self.max_step_retries > 0 && self.max_stage_retries > 0
    }
    fn has_budget(&self) -> bool {
        self.requests_started.saturating_sub(self.unavailable_failures) <= self.max_step_retries
            && self.stage_retries_used.saturating_sub(self.unavailable_retries_used)
                < self.max_stage_retries
    }
}

fn unavailable_retry_delay(failures: usize) -> i64 {
    (1_000_i64 << failures.min(9)).min(300_000)
}

fn resume_exhausted_unavailable(stored: &mut BrainstormStoredState) -> bool {
    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
    if stage.status != "failed"
        || stage.error.as_ref().and_then(|error| error["code"].as_str())
            != Some("BRAINSTORM_PREPARATION_EXHAUSTED")
    {
        return false;
    }
    let Some(recovery) = stage.automatic_recovery.as_mut() else {
        return false;
    };
    if !recovery.enabled()
        || recovery.last_failure_code.as_deref() != Some("BRAINSTORM_MODEL_UNAVAILABLE")
    {
        return false;
    }
    // Older persisted sessions predate the outage counters. Reconstruct them
    // from the current run's private audit; never forgive candidate failures.
    if recovery.unavailable_failures == 0 {
        let history = &stage.recovery_history;
        let mut run_start = 0;
        let mut last_request_by_step: BTreeMap<&str, usize> = BTreeMap::new();
        for (index, audit) in history.iter().enumerate() {
            let stage_reset = index > 0
                && audit.stage_retries_used < history[index - 1].stage_retries_used;
            let step_reset = last_request_by_step
                .get(audit.step_key.as_str())
                .is_some_and(|previous| audit.requests_started <= *previous);
            if stage_reset || step_reset {
                run_start = index;
                last_request_by_step.clear();
            }
            last_request_by_step.insert(&audit.step_key, audit.requests_started);
        }
        let run = &history[run_start..];
        let same_step: Vec<_> = run
            .iter()
            .filter(|audit| audit.step_key == recovery.step_key)
            .collect();
        let step_start = same_step
            .windows(2)
            .enumerate()
            .filter(|(_, pair)| pair[1].requests_started <= pair[0].requests_started)
            .map(|(index, _)| index + 1)
            .last()
            .unwrap_or(0);
        let outages = same_step[step_start..]
            .iter()
            .filter(|audit| audit.code == "BRAINSTORM_MODEL_UNAVAILABLE")
            .map(|audit| audit.aggregate_count.max(1))
            .sum::<usize>();
        recovery.unavailable_failures = outages.max(1).min(recovery.requests_started);
        let mut last_by_step: BTreeMap<&str, &RecoveryAudit> = BTreeMap::new();
        let mut quality_by_step: BTreeMap<&str, usize> = BTreeMap::new();
        let mut availability_retries = 0;
        for (index, audit) in run.iter().enumerate() {
            let quality_before = *quality_by_step.get(audit.step_key.as_str()).unwrap_or(&0);
            let followed_outage = last_by_step
                .get(audit.step_key.as_str())
                .is_some_and(|previous| {
                    previous.code == "BRAINSTORM_MODEL_UNAVAILABLE"
                        && audit.requests_started > previous.requests_started
                });
            if audit.requests_started > 1
                && (audit.code == "BRAINSTORM_MODEL_UNAVAILABLE"
                    || (followed_outage && quality_before == 0))
            {
                availability_retries += 1;
            }
            if audit.code == "BRAINSTORM_MODEL_UNAVAILABLE" {
                availability_retries += audit.aggregate_count.saturating_sub(1);
                // A successful dispatch has no audit entry. If this step
                // ended after outages alone and a later step has a higher
                // stage counter, the unrecorded retry finished this step.
                if quality_before == 0
                    && run.get(index + 1).is_some_and(|next| {
                        next.step_key != audit.step_key
                            && next.stage_retries_used > audit.stage_retries_used
                    })
                {
                    availability_retries += 1;
                }
            } else {
                *quality_by_step.entry(&audit.step_key).or_default() += 1;
            }
            last_by_step.insert(&audit.step_key, audit);
        }
        if run.is_empty() && recovery.requests_started > 1 {
            availability_retries = 1;
        }
        recovery.unavailable_retries_used = availability_retries.min(recovery.stage_retries_used);
    }
    recovery.request_in_flight = false;
    recovery.retry_at_ms = now();
    stage.status = "thinking".into();
    stage.error = None;
    stage.generation_id = uuid::Uuid::new_v4().to_string();
    stage.progress.updated_at_ms = now();
    stage.progress.estimated_remaining_seconds = None;
    true
}

fn recovery_exhausted(last_failure_code: Option<&str>) -> Failure {
    let message = match last_failure_code {
        Some("BRAINSTORM_MODEL_OUTPUT_INVALID") => {
            "本阶段候选未通过内容检查，已保存进度；可继续本阶段思考"
        }
        Some("BRAINSTORM_MODEL_TIMEOUT") => "本阶段思考超时，已保存进度；可继续本阶段思考",
        Some("BRAINSTORM_MODEL_UNAVAILABLE") => {
            "本阶段思考服务暂不可用，已保存进度；服务恢复后可继续本阶段思考"
        }
        Some("MODEL_RATE_LIMITED") => "本阶段请求暂时受限，已保存进度；稍后可继续本阶段思考",
        Some("BRAINSTORM_REQUEST_INTERRUPTED") => "本阶段思考被中断，已保存进度；可继续本阶段思考",
        _ => "本阶段暂未准备完成，已保存进度；可继续本阶段思考",
    };
    (
        StatusCode::BAD_GATEWAY,
        Json(serde_json::json!({
            "code": "BRAINSTORM_PREPARATION_EXHAUSTED",
            "message": message,
            "retryable": true,
        })),
    )
}

fn recovery_allowed(status: StatusCode, code: &str) -> bool {
    matches!(
        (status.as_u16(), code),
        (502, "BRAINSTORM_MODEL_OUTPUT_INVALID")
            | (504, "BRAINSTORM_MODEL_TIMEOUT")
            | (503, "BRAINSTORM_MODEL_UNAVAILABLE")
            | (429, "MODEL_RATE_LIMITED")
    )
}

fn recovery_step_key(
    req: &BrainstormTurnRequest,
    stored: &BrainstormStoredState,
) -> Result<String, Failure> {
    // Hash the actual trusted step, excluding routing credentials. Node IDs and
    // ancestor conditions are stable across retries; generation is not an input.
    let mut payload = batch_payload(req, stored)?;
    if let Some(fields) = payload.as_object_mut() {
        for key in ["creation_model", "creation_api_key", "creation_base_url"] {
            fields.remove(key);
        }
    }
    Ok(sha256_hex(&payload.to_string()))
}

fn reserve_recovery_dispatch(
    stored: &mut BrainstormStoredState,
    req: &BrainstormTurnRequest,
) -> Result<bool, Failure> {
    let step_key = recovery_step_key(req, stored)?;
    let authority_fingerprint = input_fingerprint(req, stored);
    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
    let recovery = stage
        .automatic_recovery
        .get_or_insert_with(AutomaticRecovery::configured);
    if !recovery.enabled() {
        return Ok(false);
    }
    if recovery.authority_fingerprint != authority_fingerprint {
        // A real new user/skill/permission input is a new bounded preparation,
        // while dispatches, generations, successful nodes and restarts are not.
        recovery.authority_fingerprint = authority_fingerprint;
        recovery.stage_retries_used = 0;
        recovery.unavailable_retries_used = 0;
        recovery.step_key.clear();
    }
    if recovery.step_key != step_key {
        recovery.step_key = step_key;
        recovery.requests_started = 0;
        recovery.unavailable_failures = 0;
        recovery.retry_at_ms = 0;
        recovery.request_in_flight = false;
        recovery.last_failure_code = None;
    }
    if recovery.requests_started > 0 {
        if recovery.request_in_flight {
            // A restart never grants a free duplicate request. Its abandoned
            // dispatch is evidence, and a new dispatch uses remaining budget.
            stage.recovery_history.push(RecoveryAudit {
                step_key: recovery.step_key.clone(),
                code: "BRAINSTORM_REQUEST_INTERRUPTED".into(),
                http_status: 503,
                failed_at_ms: now(),
                requests_started: recovery.requests_started,
                stage_retries_used: recovery.stage_retries_used,
                aggregate_count: 0,
            });
            recovery.last_failure_code = Some("BRAINSTORM_REQUEST_INTERRUPTED".into());
        }
        let retry_after_unavailable = recovery.last_failure_code.as_deref()
            == Some("BRAINSTORM_MODEL_UNAVAILABLE");
        let free_first_quality_attempt = retry_after_unavailable
            && recovery.requests_started.saturating_sub(recovery.unavailable_failures) == 0;
        if free_first_quality_attempt {
            recovery.unavailable_retries_used += 1;
        }
        if !recovery.has_budget() && !retry_after_unavailable {
            if free_first_quality_attempt {
                recovery.unavailable_retries_used -= 1;
            }
            return Err(recovery_exhausted(recovery.last_failure_code.as_deref()));
        }
        recovery.stage_retries_used += 1;
    }
    recovery.requests_started += 1;
    recovery.request_in_flight = true;
    recovery.retry_at_ms = 0;
    Ok(true)
}

fn record_preparation_failure(
    stored: &mut BrainstormStoredState,
    mut status: StatusCode,
    mut error: serde_json::Value,
    dispatched: bool,
) {
    normalize_stage_error(&mut error);
    if !dispatched && error["code"] == "BRAINSTORM_MODEL_OUTPUT_INVALID" {
        status = StatusCode::CONFLICT;
        error = serde_json::json!({"code":"BRAINSTORM_PREPARATION_INPUT_INVALID",
            "message":"当前阶段配置或输入已失效，已保存进度；请检查配置或输入后继续",
            "retryable":false});
    }
    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
    let audit_step_key = sha256_hex(
        &serde_json::json!({
            "stage": stage.id, "completed_batches": stage.progress.completed_batches,
            "topics": stage.topics, "topic_checkpoint": stage.topic_checkpoint,
            "review_checkpoint": stage.review_checkpoint,
        })
        .to_string(),
    );
    let code = error["code"]
        .as_str()
        .unwrap_or("BRAINSTORM_UNKNOWN_FAILURE")
        .to_string();
    let recovery = stage
        .automatic_recovery
        .get_or_insert_with(AutomaticRecovery::configured);
    if !dispatched || recovery.step_key.is_empty() {
        recovery.step_key = audit_step_key;
    }
    let prior_failure_was_unavailable =
        recovery.last_failure_code.as_deref() == Some("BRAINSTORM_MODEL_UNAVAILABLE");
    let prior_quality_attempts = recovery
        .requests_started
        .saturating_sub(1)
        .saturating_sub(recovery.unavailable_failures);
    recovery.request_in_flight = false;
    recovery.last_failure_code = Some(code.clone());
    if dispatched && code == "BRAINSTORM_MODEL_UNAVAILABLE" {
        recovery.unavailable_failures += 1;
        if recovery.requests_started > 1
            && !(prior_failure_was_unavailable && prior_quality_attempts == 0)
        {
            recovery.unavailable_retries_used += 1;
        }
    }
    let audit = RecoveryAudit {
        step_key: recovery.step_key.clone(),
        code: code.clone(),
        http_status: status.as_u16(),
        failed_at_ms: now(),
        requests_started: recovery.requests_started,
        stage_retries_used: recovery.stage_retries_used,
        aggregate_count: usize::from(code == "BRAINSTORM_MODEL_UNAVAILABLE"),
    };
    if code == "BRAINSTORM_MODEL_UNAVAILABLE"
        && stage.recovery_history.last().is_some_and(|previous| {
            previous.code == code
                && previous.step_key == audit.step_key
                && previous.aggregate_count > 0
        })
    {
        let previous = stage.recovery_history.last_mut().unwrap();
        previous.aggregate_count = previous.aggregate_count.saturating_add(1);
        previous.failed_at_ms = audit.failed_at_ms;
        previous.requests_started = audit.requests_started;
        previous.stage_retries_used = audit.stage_retries_used;
    } else {
        stage.recovery_history.push(audit);
    }
    stage.progress.updated_at_ms = now();
    if dispatched
        && recovery.enabled()
        && recovery_allowed(status, &code)
        && (code == "BRAINSTORM_MODEL_UNAVAILABLE" || recovery.has_budget())
    {
        stage.status = "thinking".into();
        stage.error = None;
        stage.progress.estimated_remaining_seconds = None;
        let delay = if code == "BRAINSTORM_MODEL_UNAVAILABLE" {
            unavailable_retry_delay(recovery.unavailable_failures)
        } else if code == "MODEL_RATE_LIMITED" {
            5_000
        } else {
            1_000
        };
        recovery.retry_at_ms = now() + if code == "BRAINSTORM_MODEL_UNAVAILABLE" {
            delay
        } else {
            delay * recovery.requests_started.max(1) as i64
        };
    } else {
        stage.status = "failed".into();
        stage.error = Some(
            if dispatched && recovery.enabled() && recovery_allowed(status, &code) {
                recovery_exhausted(Some(&code)).1 .0
            } else {
                error
            },
        );
        recovery.retry_at_ms = 0;
    }
}

fn review_chunk<'a>(
    stage: &'a Stage,
    checkpoint: &PreparationReviewCheckpoint,
) -> &'a [BrainstormQuestion] {
    let start = checkpoint.next_index.min(stage.questions.len());
    let mut end = start;
    let mut units: usize = 0;
    for question in stage
        .questions
        .iter()
        .skip(start)
        .take(MAX_REVIEW_QUESTIONS)
    {
        // Sidecar checks the prompt, decision space, and every actual option.
        let cost = 2usize.saturating_add(question.options.len());
        if end > start && units.saturating_add(cost) > checkpoint.unit_budget {
            break;
        }
        units = units.saturating_add(cost);
        end += 1;
    }
    &stage.questions[start..end]
}

fn stage_limits() -> serde_json::Value {
    serde_json::json!({
        "max_topics": config("MEMORYBREAD_BRAINSTORM_STAGE_MAX_TOPICS", 6, 12),
        "max_questions_per_topic": config("MEMORYBREAD_BRAINSTORM_STAGE_MAX_QUESTIONS", 12, 24),
        "max_depth": config("MEMORYBREAD_BRAINSTORM_STAGE_MAX_DEPTH", 2, 3).max(2),
    })
}

impl StageWorkflow {
    fn new() -> Self {
        Self {
            version: 2,
            active_stage: "direction".into(),
            active_chapter_id: None,
            chapters: vec![],
            copy_repair_history: vec![],
            input_return_stage: None,
            stages: [
                ("direction", "创作方向脑暴"),
                ("detail", "创作细节脑暴"),
                ("writing", "创作行文脑暴"),
            ]
            .into_iter()
            .map(|(id, title)| Stage {
                preparation_contract_version: PREPARATION_CONTRACT_VERSION,
                content_mode: false,
                id: id.into(),
                title: title.into(),
                status: if id == "direction" {
                    "thinking"
                } else {
                    "pending"
                }
                .into(),
                generation_id: uuid::Uuid::new_v4().to_string(),
                questions: vec![],
                input_preflight: None,
                input_phase: String::new(),
                input_attempt: 0,
                confirmed_period: None,
                topics: vec![],
                topic_checkpoint: None,
                topic_checkpoint_archives: vec![],
                review_checkpoint: None,
                preparation_archives: vec![],
                catalog_binding: None,
                root_axes_binding: if id == "writing" {
                    None
                } else {
                    root_axes::load().ok()
                },
                preparation_progress: None,
                recovery_progress: None,
                automatic_recovery: None,
                recovery_history: vec![],
                content_proofs: vec![],
                content_no_new_decision_reason: None,
                content_audit_sha256: None,
                content_archives: vec![],
                content_brief_edit_archives: vec![],
                content_root_request_archives: vec![],
                error: None,
                progress: Progress {
                    completed_batches: 0,
                    total_batches: 0,
                    percent: 0,
                    started_at_ms: now(),
                    updated_at_ms: now(),
                    estimated_remaining_seconds: None,
                },
            })
            .collect(),
        }
    }
    fn new_content() -> Self {
        let mut workflow = Self::new();
        for stage in &mut workflow.stages {
            stage.content_mode = true;
            stage.preparation_contract_version = CONTENT_PREPARATION_CONTRACT_VERSION;
            stage.root_axes_binding = None;
        }
        workflow
    }
    fn new_for_selected_skills(selected_skills: &[serde_json::Value]) -> Self {
        if selected_skills.is_empty() {
            Self::new_content()
        } else {
            // Trusted Skill catalog questions and writing structure stay bound
            // to the existing compiled catalog contract.
            Self::new()
        }
    }
    fn current(&self) -> &Stage {
        self.stages
            .iter()
            .find(|s| s.id == self.active_stage)
            .expect("validated stage")
    }
    fn current_mut(&mut self) -> &mut Stage {
        self.stages
            .iter_mut()
            .find(|s| s.id == self.active_stage)
            .expect("validated stage")
    }
    pub(super) fn is_content_question(&self, question: &BrainstormQuestion) -> bool {
        question.input_clarification.is_none() && self.stages.iter().any(|stage| {
            stage.content_mode
                && stage.preparation_contract_version == CONTENT_PREPARATION_CONTRACT_VERSION
                && stage.questions.iter().any(|saved| saved.id == question.id)
        })
    }
    pub(super) fn for_response(&self) -> Self {
        let mut result = self.clone();
        for stage in &mut result.stages {
            stage.recovery_progress = stage
                .automatic_recovery
                .as_ref()
                .filter(|recovery| {
                    stage.status == "thinking" && recovery.last_failure_code.is_some()
                })
                .map(|recovery| RecoveryProgress {
                    attempt: if recovery.request_in_flight {
                        recovery.requests_started
                            .saturating_sub(recovery.unavailable_failures)
                            .saturating_sub(1)
                    } else {
                        recovery.requests_started.saturating_sub(recovery.unavailable_failures)
                    },
                    max_attempts: recovery.max_step_retries,
                    retry_at_ms: recovery.retry_at_ms,
                });
            stage.automatic_recovery = None;
            stage.recovery_history.clear();
            stage.content_mode = false;
            stage.content_proofs.clear();
            stage.content_audit_sha256 = None;
            stage.content_archives.clear();
            stage.content_brief_edit_archives.clear();
            stage.content_root_request_archives.clear();
            stage.preparation_progress =
                stage
                    .topic_checkpoint
                    .as_ref()
                    .map(|checkpoint| PreparationProgress {
                        topic_title: checkpoint.topic.title.clone(),
                        saved_nodes: checkpoint.questions.len(),
                        pending_nodes: checkpoint.frontier.len(),
                    });
            stage.topic_checkpoint = None;
            stage.topic_checkpoint_archives.clear();
            stage.review_checkpoint = None;
            stage.preparation_archives.clear();
            stage.catalog_binding = None;
            stage.root_axes_binding = None;
            for topic in &mut stage.topics {
                topic.root_axis_ids.clear();
            }
        }
        result
            .input_return_stage = None;
        result
    }
}

fn decode(session: &repo::CreationBrainstormSession) -> Result<BrainstormStoredState, Failure> {
    let mut stored: BrainstormStoredState =
        serde_json::from_str(&session.state_json).map_err(|_| {
            brainstorm_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "BRAINSTORM_STATE_INVALID",
                "已有脑暴进度无法恢复",
            )
        })?;
    let Some(w) = stored.stage_workflow.as_mut() else {
        return Err(invalid("当前会话尚未启用三阶段脑暴"));
    };
    if w.version != 2
        || w.stages.len() != 3
        || !["direction", "detail", "writing"]
            .iter()
            .all(|id| w.stages.iter().filter(|s| s.id == *id).count() == 1)
        || !w.stages.iter().any(|s| s.id == w.active_stage)
    {
        return Err(invalid("脑暴阶段状态无法恢复，请保留会话并重试"));
    }
    // Older stage failures inherited the legacy turn's non-retryable output
    // error. An explicit retry can safely continue the failed saved batch;
    // status reads must not restart it or rewrite the session's history.
    for stage in &mut w.stages {
        if let Some(error) = &mut stage.error {
            normalize_stage_error(error);
        }
        if stage.content_mode && !content_audit_valid(stage) {
            return Err(brainstorm_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "BRAINSTORM_STATE_INVALID",
                "已有脑暴内容题进度无法核验",
            ));
        }
    }
    Ok(stored)
}

fn content_audit_valid(stage: &Stage) -> bool {
    if stage.preparation_contract_version != CONTENT_PREPARATION_CONTRACT_VERSION
        || stage.catalog_binding.is_some()
        || stage.root_axes_binding.is_some()
    {
        return false;
    }
    if stage.content_archives.iter().any(|archive| {
        archive.content_audit_sha256
            != content_audit_hash(
                &archive.content_proofs,
                &archive.content_no_new_decision_reason,
            )
    }) || stage.content_brief_edit_archives.iter().any(|archive| {
        archive.field_id.is_empty()
            || archive.reason.is_empty()
            || archive.sha256 != content_brief_edit_hash(
                &archive.field_id, &archive.text, archive.saved_revision, &archive.reason,
            )
    }) || stage.content_root_request_archives.iter().any(|archive| {
        archive.previous_root_request.trim().is_empty()
            || archive.new_root_input.trim().is_empty()
            || archive.sha256 != content_root_request_hash(
                &archive.previous_root_request,
                &archive.previous_root_input,
                archive.previous_input_revision,
                &archive.new_root_input,
                archive.new_input_revision,
            )
    }) {
        return false;
    }
    let Some(expected) = stage.content_audit_sha256.as_ref() else {
        return stage.progress.completed_batches == 0
            && stage.content_proofs.is_empty()
            && stage.content_no_new_decision_reason.is_none();
    };
    if stage.progress.completed_batches != 1
        || stage.progress.total_batches != 1
        || stage.content_proofs.len() > 4
        || (stage.content_proofs.is_empty() == stage.content_no_new_decision_reason.is_none())
    {
        return false;
    }
    let mut ids = HashSet::new();
    if stage.content_proofs.iter().any(|proof| {
        !ids.insert(&proof.question_id)
            || !stage.questions.iter().any(|question| question.id == proof.question_id)
    }) {
        return false;
    }
    let actual = content_audit_hash(&stage.content_proofs, &stage.content_no_new_decision_reason);
    &actual == expected
}

fn content_audit_hash(
    proofs: &[ContentQuestionProof],
    no_new_decision_reason: &Option<String>,
) -> String {
    sha256_hex(&serde_json::json!({
        "content_proofs": proofs,
        "no_new_decision_reason": no_new_decision_reason,
    }).to_string())
}

fn content_brief_edit_hash(
    field_id: &str,
    value: &str,
    saved_revision: Option<i64>,
    reason: &str,
) -> String {
    sha256_hex(&serde_json::json!({
        "field_id": field_id,
        "text": value,
        "saved_revision": saved_revision,
        "reason": reason,
    }).to_string())
}

fn content_root_request_hash(
    previous_root_request: &str,
    previous_root_input: &Option<String>,
    previous_input_revision: Option<i64>,
    new_root_input: &str,
    new_input_revision: i64,
) -> String {
    sha256_hex(&serde_json::json!({
        "previous_root_request": previous_root_request,
        "previous_root_input": previous_root_input,
        "previous_input_revision": previous_input_revision,
        "new_root_input": new_root_input,
        "new_input_revision": new_input_revision,
    }).to_string())
}

fn archive_replaced_content_root_request(
    stage: &mut Stage,
    previous_root_request: &str,
    previous_root_input: Option<&String>,
    previous_input_revision: Option<i64>,
    new_root_input: &str,
    new_input_revision: i64,
) {
    // The original request remains in the session's immutable SQL root column.
    // Archive only a later replacement that column cannot recover.
    if !stage.content_mode
        || (previous_input_revision.unwrap_or(0) == 0 && previous_root_input.is_none())
    {
        return;
    }
    let previous_root_input = previous_root_input.cloned();
    if stage.content_root_request_archives.iter().any(|archive| {
        archive.previous_root_request == previous_root_request
            && archive.previous_root_input == previous_root_input
            && archive.previous_input_revision == previous_input_revision
            && archive.new_root_input == new_root_input
            && archive.new_input_revision == new_input_revision
    }) {
        return;
    }
    stage.content_root_request_archives.push(ContentRootRequestArchive {
        previous_root_request: previous_root_request.into(),
        previous_root_input: previous_root_input.clone(),
        previous_input_revision,
        new_root_input: new_root_input.into(),
        new_input_revision,
        archived_at_ms: now(),
        sha256: content_root_request_hash(
            previous_root_request,
            &previous_root_input,
            previous_input_revision,
            new_root_input,
            new_input_revision,
        ),
    });
}

fn archive_content_brief_edits(
    stage: &mut Stage,
    brief_edits: &BTreeMap<String, String>,
    revisions: &BTreeMap<String, i64>,
    fields: &HashSet<String>,
    reason: &str,
) {
    if !stage.content_mode {
        return;
    }
    let mut fields = fields.iter().collect::<Vec<_>>();
    fields.sort();
    for field_id in fields {
        let Some(value) = brief_edits.get(field_id) else { continue };
        let saved_revision = revisions.get(field_id).copied();
        if stage.content_brief_edit_archives.iter().any(|archive|
            archive.field_id == field_id.as_str()
                && archive.text == *value
                && archive.saved_revision == saved_revision
                && archive.reason == reason)
        {
            continue;
        }
        stage.content_brief_edit_archives.push(ContentBriefEditArchive {
            field_id: (*field_id).clone(),
            text: value.clone(),
            saved_revision,
            reason: reason.into(),
            archived_at_ms: now(),
            sha256: content_brief_edit_hash(field_id, value, saved_revision, reason),
        });
    }
}

fn archive_content_preparation(stage: &mut Stage, reason: &str) {
    if let Some(content_audit_sha256) = stage.content_audit_sha256.take() {
        let ids: HashSet<_> = stage.content_proofs.iter()
            .map(|proof| proof.question_id.as_str()).collect();
        stage.content_archives.push(ContentPreparationArchive {
            reason: reason.into(),
            archived_at_ms: now(),
            questions: stage.questions.iter().filter(|question| ids.contains(question.id.as_str()))
                .cloned().collect(),
            content_proofs: std::mem::take(&mut stage.content_proofs),
            content_no_new_decision_reason: stage.content_no_new_decision_reason.take(),
            content_audit_sha256,
        });
    }
    stage.content_proofs.clear();
    stage.content_no_new_decision_reason = None;
}

fn content_pool_has_user_work(stored: &BrainstormStoredState, stage: &Stage) -> bool {
    let ids: HashSet<_> = stage.content_proofs.iter()
        .map(|proof| proof.question_id.as_str()).collect();
    stored.turns.iter().any(|turn| ids.contains(turn.question.id.as_str()))
        || stored.brief_edits.keys().any(|id| ids.contains(id.as_str()))
        || stored.stage_workflow.as_ref().unwrap().copy_repair_history.iter()
            .any(|repair| ids.contains(repair.question_id.as_str()))
}

fn normalize_stage_error(error: &mut serde_json::Value) {
    if error["code"] == "BRAINSTORM_MODEL_OUTPUT_INVALID" {
        error["retryable"] = serde_json::json!(true);
    }
}

fn persist(
    state: &AppState,
    session: &str,
    revision: i64,
    phase: &str,
    stored: &BrainstormStoredState,
) -> Result<(), Failure> {
    let mut catalog_changed = false;
    let mut root_axes_changed = false;
    let changed = with_brainstorm_session_guard(state, session, phase == "abandoned", |conn| {
        let workflow = stored.stage_workflow.as_ref().unwrap();
        if let Some(expected) = &workflow.current().catalog_binding {
            if review_is_allowed(stored) {
                let limits = BuildLimits::configured();
                let actual = brainstorm_catalog::resolve_on_connection(
                    conn,
                    &stored.selected_skills,
                    &workflow.active_stage,
                    stage_limits()["max_topics"].as_u64().unwrap() as usize,
                    limits.max_questions_per_topic,
                    limits.max_depth,
                );
                if actual.as_ref().ok().and_then(Option::as_ref) != Some(expected) {
                    catalog_changed = true;
                    return Ok(false);
                }
            }
        }
        if root_axes_refresh_allowed(stored) {
            if let Some(expected) = &workflow.current().root_axes_binding {
                if root_axes::load().as_ref().ok() != Some(expected) {
                    root_axes_changed = true;
                    return Ok(false);
                }
            }
        }
        Ok(repo::update(
            conn,
            session,
            revision,
            phase,
            &serde_json::to_string(stored)?,
        )?)
    })?;
    if catalog_changed {
        return Err(catalog_invalid());
    }
    if root_axes_changed {
        return Err(root_axes_invalid());
    }
    if !changed {
        return Err(brainstorm_error(
            StatusCode::CONFLICT,
            "BRAINSTORM_REVISION_CONFLICT",
            "脑暴内容已更新，请刷新后重试",
        ));
    }
    Ok(())
}

// A question is reachable only along selected edges. Excluded, skipped and
// custom branches require no speculative inference and cannot activate siblings.
fn next_question(stored: &BrainstormStoredState) -> Option<BrainstormQuestion> {
    let w = stored.stage_workflow.as_ref()?;
    let stage = w.current();
    let chapter = w
        .active_chapter_id
        .as_ref()
        .and_then(|id| w.chapters.iter().find(|c| &c.id == id));
    stage
        .questions
        .iter()
        .filter(|q| {
            if w.active_stage == "writing"
                && !chapter.is_some_and(|c| c.question_ids.contains(&q.id))
            {
                return false;
            }
            if stored.turns.iter().any(|t| t.question.id == q.id) {
                return false;
            }
            let mut node = *q;
            let mut visited = HashSet::new();
            while let Some(parent_id) = node.parent_question_id.as_ref() {
                if !visited.insert(parent_id) {
                    return false;
                }
                let Some(turn) = stored.turns.iter().find(|t| &t.question.id == parent_id) else {
                    return false;
                };
                if turn.answer.source != "user"
                    || !node
                        .parent_option_id
                        .as_ref()
                        .is_some_and(|id| turn.answer.selected_option_ids.contains(id))
                {
                    return false;
                }
                node = &turn.question;
            }
            true
        })
        .min_by_key(|q| {
            let mut depth = 0;
            let mut node = *q;
            while let Some(parent_id) = &node.parent_question_id {
                depth += 1;
                if depth > stage.questions.len() {
                    break;
                }
                let Some(parent) = stage.questions.iter().find(|p| &p.id == parent_id) else {
                    break;
                };
                node = parent;
            }
            depth
        })
        .cloned()
}

fn select_next(stored: &mut BrainstormStoredState) -> String {
    stored.current_question = next_question(stored);
    let w = stored.stage_workflow.as_mut().unwrap();
    let status = if stored.current_question.is_some() {
        "answering"
    } else {
        "ready"
    };
    w.current_mut().status = status.into();
    if w.active_stage == "writing" {
        if let Some(chapter) = w
            .chapters
            .iter_mut()
            .find(|c| Some(&c.id) == w.active_chapter_id.as_ref())
        {
            chapter.status = status.into();
        }
    }
    if status == "ready" {
        "ready"
    } else {
        "exploring"
    }
    .into()
}

fn selected_input_action(question: &BrainstormQuestion, answer: &BrainstormAnswer) -> Option<String> {
    let id = answer.selected_option_ids.first()?;
    question.options.iter().find(|option| &option.id == id)?
        .input_value.as_ref()?["action"].as_str().map(str::to_owned)
}

fn confirmed_answer_period(
    question: &BrainstormQuestion,
    answer: &BrainstormAnswer,
    timezone: &str,
) -> Result<serde_json::Value, Failure> {
    let (start, end, zone) = if let Some(id) = answer.selected_option_ids.first() {
        let value = question.options.iter().find(|option| &option.id == id)
            .and_then(|option| option.input_value.as_ref())
            .ok_or_else(|| invalid("请重新选择有效的报告周期"))?;
        (value["start"].as_str().unwrap_or_default().to_owned(),
         value["end"].as_str().unwrap_or_default().to_owned(),
         value["timezone"].as_str().unwrap_or(timezone).to_owned())
    } else {
        let entered = answer.custom_text.trim();
        let parts = ["至", "到", "～", "~", "—", "–"]
            .iter().find_map(|delimiter| entered.split_once(delimiter));
        let (start, end) = parts.ok_or_else(|| invalid("请填写完整的起止日期，例如 2026-09-21 至 2026-09-27"))?;
        (start.trim().to_owned(), end.trim().to_owned(), timezone.to_owned())
    };
    let start_day = chrono::NaiveDate::parse_from_str(&start, "%Y-%m-%d")
        .map_err(|_| invalid("报告起始日期无效，请使用 YYYY-MM-DD"))?;
    let end_day = chrono::NaiveDate::parse_from_str(&end, "%Y-%m-%d")
        .map_err(|_| invalid("报告结束日期无效，请使用 YYYY-MM-DD"))?;
    if start_day > end_day || (end_day - start_day).num_days() > 370 || zone.trim().is_empty() {
        return Err(invalid("报告周期范围无效，请核对起止日期"));
    }
    Ok(serde_json::json!({"start":start,"end":end,"timezone":zone,"source":"user"}))
}

fn detail_input_ready(stored: &BrainstormStoredState) -> bool {
    stored.stage_workflow.as_ref().and_then(|workflow| workflow.stages.iter().find(|s| s.id == "detail"))
        .is_some_and(|stage| stage.input_phase == "complete" && stage.input_preflight.is_some())
}

fn needs_input_preflight(stored: &BrainstormStoredState) -> bool {
    if ["周报", "月报", "季度报", "weekly report", "monthly report"]
        .iter().any(|word| stored.root_request.to_lowercase().contains(word)) {
        return true;
    }
    stored.selected_skills.iter().any(|skill| {
        skill["executionSteps"].as_array()
            .or_else(|| skill["execution_steps"].as_array())
            .is_some_and(|steps| steps.iter().any(|step| {
                step["tools"].as_array().is_some_and(|tools| tools.iter().any(|tool| {
                    matches!(tool.as_str(), Some("memory_search" | "data_search"))
                }))
            }))
    })
}

pub(super) fn candidate_copy_is_complete(text: &str) -> bool {
    let ending = text.trim_end_matches(|character: char| {
        character.is_whitespace()
            || matches!(
                character,
                '。' | '！'
                    | '？'
                    | '!'
                    | '?'
                    | '；'
                    | ';'
                    | '，'
                    | ','
                    | '：'
                    | ':'
                    | '.'
                    | '…'
                    | '"'
                    | '\''
                    | '”'
                    | '’'
                    | '）'
                    | ')'
                    | ']'
                    | '】'
                    | '》'
                    | '〉'
            )
    });
    let complete_yu_words = ["参与", "赠与", "授与", "付与", "施与", "相与", "给与"];
    if ["以及", "并且", "或者"]
        .iter()
        .any(|word| ending.ends_with(word))
        || (ending.ends_with("与") && !complete_yu_words.iter().any(|word| ending.ends_with(word)))
    {
        return false;
    }
    if let Some(range) = ending.strip_suffix("至") {
        let range = range.trim_end();
        let numeric = [
            "季度", "小时", "分钟", "秒钟", "年", "月", "日", "时", "分", "秒", "周", "天", "%",
            "％",
        ]
        .iter()
        .find_map(|unit| range.strip_suffix(unit))
        .unwrap_or(range);
        if numeric.chars().next_back().is_some_and(|character| {
            character.is_ascii_digit() || ('０'..='９').contains(&character)
        }) {
            return false;
        }
    }
    let bytes = text.as_bytes();
    for start in 0..bytes.len() {
        if start > 0 && bytes[start - 1].is_ascii_alphanumeric() {
            continue;
        }
        if start + 6 > bytes.len()
            || !bytes[start..start + 4].iter().all(u8::is_ascii_digit)
            || bytes[start + 4] != b'-'
        {
            continue;
        }
        let mut end = start + 5;
        while end < bytes.len() && end < start + 7 && bytes[end].is_ascii_digit() {
            end += 1;
        }
        if end > start + 5
            && end < bytes.len()
            && bytes[end] == b'-'
            && (end + 1 == bytes.len()
                || (!bytes[end + 1].is_ascii_alphanumeric() && bytes[end + 1] != b'-'))
        {
            return false;
        }
    }
    true
}

fn candidate_label_key(label: &str) -> String {
    label
        .chars()
        .filter(|character| !character.is_whitespace())
        .flat_map(char::to_lowercase)
        .collect()
}

fn repair_candidate_copy(
    stored: &mut BrainstormStoredState,
    req: &BrainstormTurnRequest,
    revision: i64,
) -> Result<String, Failure> {
    let original_stage = stored.stage_workflow.as_ref().unwrap().current();
    if original_stage.content_mode {
        return Err(invalid("当前内容题已完成来源与质量校验，暂不支持直接修订候选文案；请使用自定义答案，或修订原始需求后重新生成问题"));
    }
    if original_stage.status != "answering" {
        return Err(invalid("仅能修订当前回答阶段中尚未确认的候选文案"));
    }
    if req.candidate_copy_patches.is_empty() || req.candidate_copy_patches.len() > 16 {
        return Err(invalid("候选文案修订每次须包含 1 至 16 个字段"));
    }
    let mut targets = HashSet::new();
    let mut label_questions = HashSet::new();
    let mut revised = stored.clone();
    let mut repairs = vec![];
    for patch in &req.candidate_copy_patches {
        if !targets.insert((
            patch.question_id.clone(),
            patch.option_id.clone(),
            patch.field,
        )) {
            return Err(invalid("同一候选文案字段不能重复修订"));
        }
        let question_index = original_stage
            .questions
            .iter()
            .position(|question| question.id == patch.question_id)
            .ok_or_else(|| invalid("候选题不属于当前回答阶段"))?;
        let question = &original_stage.questions[question_index];
        let mut descendants: HashSet<String> = [question.id.clone()].into_iter().collect();
        loop {
            let previous = descendants.len();
            for candidate in &original_stage.questions {
                if candidate
                    .parent_question_id
                    .as_ref()
                    .is_some_and(|id| descendants.contains(id))
                {
                    descendants.insert(candidate.id.clone());
                }
            }
            if descendants.len() == previous {
                break;
            }
        }
        if stored
            .turns
            .iter()
            .any(|turn| descendants.contains(&turn.question.id))
        {
            return Err(invalid("候选题或其下钻题已有回答，不能修订其文案"));
        }
        let (option_index, old_text, maximum) = match patch.field {
            CandidateCopyField::Label => {
                let option_id = patch
                    .option_id
                    .as_deref()
                    .ok_or_else(|| invalid("选项名称修订必须指定 option_id"))?;
                let index = question
                    .options
                    .iter()
                    .position(|option| option.id == option_id)
                    .ok_or_else(|| invalid("候选选项不存在"))?;
                label_questions.insert(question.id.clone());
                (Some(index), question.options[index].label.as_str(), 24)
            }
            CandidateCopyField::WhyNow => {
                if patch.option_id.is_some() {
                    return Err(invalid("提问理由修订不能指定 option_id"));
                }
                (None, question.why_now.as_str(), 80)
            }
        };
        if old_text != patch.expected_text {
            return Err(invalid("候选文案已变化，请刷新后再次修订"));
        }
        let text = patch.text.trim();
        if text.is_empty() || text.chars().count() > maximum || !candidate_copy_is_complete(text) {
            return Err(invalid("候选文案须完整且符合字段长度限制"));
        }
        if text == old_text {
            return Err(invalid("候选文案没有变化，无需修订"));
        }
        let destination = &mut revised
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .questions[question_index];
        match option_index {
            Some(index) => destination.options[index].label = text.into(),
            None => destination.why_now = text.into(),
        }
        if let Some(current) = revised
            .current_question
            .as_mut()
            .filter(|current| current.id == question.id)
        {
            match patch.field {
                CandidateCopyField::Label => {
                    let option = current
                        .options
                        .iter_mut()
                        .find(|option| Some(&option.id) == patch.option_id.as_ref())
                        .ok_or_else(|| invalid("当前问题快照缺少候选选项，请刷新后重试"))?;
                    if option.label != old_text {
                        return Err(invalid("当前问题快照文案不一致，请刷新后重试"));
                    }
                    option.label = text.into();
                }
                CandidateCopyField::WhyNow => {
                    if current.why_now != old_text {
                        return Err(invalid("当前问题快照文案不一致，请刷新后重试"));
                    }
                    current.why_now = text.into();
                }
            }
        }
        repairs.push(CandidateCopyRepair {
            revision,
            question_id: patch.question_id.clone(),
            option_id: patch.option_id.clone(),
            field: patch.field,
            old_text: old_text.into(),
            new_text: text.into(),
        });
    }
    let revised_stage = revised.stage_workflow.as_ref().unwrap().current();
    for question in &revised_stage.questions {
        if label_questions.contains(&question.id) {
            let keys: HashSet<_> = question
                .options
                .iter()
                .map(|option| candidate_label_key(&option.label))
                .collect();
            if keys.len() != question.options.len() {
                return Err(invalid("修订后的选项名称不能与同题其他选项重复"));
            }
        }
    }
    for patch in req
        .candidate_copy_patches
        .iter()
        .filter(|patch| patch.field == CandidateCopyField::Label)
    {
        if revised_stage.questions.iter().any(|question| {
            question.parent_question_id.as_deref() == Some(patch.question_id.as_str())
                && question.parent_option_id.as_ref() == patch.option_id.as_ref()
                && !question.prompt.contains(patch.text.trim())
        }) {
            return Err(invalid("新选项名称须与已保存下钻题的条件一致"));
        }
    }
    revised
        .stage_workflow
        .as_mut()
        .unwrap()
        .copy_repair_history
        .extend(repairs);
    *stored = revised;
    Ok("exploring".into())
}

pub(super) async fn turn(
    state: Arc<AppState>,
    req: &BrainstormTurnRequest,
    existing: Option<repo::CreationBrainstormSession>,
) -> Result<Json<BrainstormTurnResponse>, Failure> {
    let session_id = req.session_id.trim();
    let mut session = match existing {
        Some(session) => session,
        None => {
            if req.action != "start" {
                return Err(invalid("请先开始脑暴"));
            }
            let mut stored: BrainstormStoredState = serde_json::from_value(
                serde_json::json!({"root_request": req.root_request.trim()}),
            )
            .unwrap();
            stored.selected_skills = req.selected_skills.clone();
            stored.skill_binding_version = 1;
            stored.work_context = brainstorm_initial_work_context(req)?;
            stored.stage_workflow = Some(StageWorkflow::new_for_selected_skills(&stored.selected_skills));
            stored.user_input_revisions.insert("root_request".into(), 0);
            with_brainstorm_session_guard(&state, session_id, false, |conn| {
                // Two windows can read a missing session before either writes.
                // The connection lock makes start idempotent before inference.
                if let Some(current) = repo::get(conn, session_id)? {
                    return Ok(current);
                }
                Ok(repo::create(
                    conn,
                    session_id,
                    &stored.root_request,
                    "preparing",
                    &serde_json::to_string(&stored)?,
                )?)
            })?
        }
    };
    if session.phase == "abandoned" && req.action != "abandon" {
        return Err(brainstorm_session_terminated_error());
    }
    let mut stored = decode(&session)?;
    let stage = stored.stage_workflow.as_ref().unwrap().current();
    let axes_changed = !stage.content_mode && matches!(
        req.action.as_str(),
        "answer" | "skip" | "exclude" | "confirm_stage"
    ) && root_axes_refresh_allowed(&stored)
        && root_axes::load()
            .map_err(|_| root_axes_invalid())?
            .config_sha256
            != stage
                .root_axes_binding
                .as_ref()
                .map(|binding| binding.config_sha256.clone())
                .unwrap_or_default();
    let upgrade_before_answer = !stage.content_mode &&
        (stage.preparation_contract_version != PREPARATION_CONTRACT_VERSION || axes_changed)
            && !stage.questions.is_empty()
            && review_is_allowed(&stored)
            && matches!(
                req.action.as_str(),
                "answer" | "skip" | "exclude" | "confirm_stage"
            );
    if matches!(req.action.as_str(), "start" | "status") || upgrade_before_answer {
        if upgrade_before_answer && req.revision != Some(session.revision) {
            return Err(brainstorm_error(
                StatusCode::CONFLICT,
                "BRAINSTORM_REVISION_CONFLICT",
                "脑暴内容已更新，请刷新后重试",
            ));
        }
        let generator_changed = refresh_generator_contract(&mut stored)?;
        let catalog_changed = refresh_catalog_binding(&state, &mut stored)?;
        let root_axes_changed = refresh_root_axes_binding(&mut stored)?;
        let checkpoint_changed = refresh_checkpoint_input(&mut stored, req);
        let review_changed = prepare_pool_review(&mut stored, req)?;
        let unavailable_resumed = resume_exhausted_unavailable(&mut stored);
        if generator_changed
            || catalog_changed
            || root_axes_changed
            || checkpoint_changed
            || review_changed
            || unavailable_resumed
        {
            let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
            // Invalidate the old worker before a new request is scheduled.
            // The archive and generation switch commit in the same CAS write.
            stage.generation_id = uuid::Uuid::new_v4().to_string();
            stage.progress.updated_at_ms = now();
            if stage.status == "thinking" {
                session.phase = "preparing".into();
            }
            persist(
                &state,
                session_id,
                session.revision,
                &session.phase,
                &stored,
            )?;
            session.revision += 1;
        }
        schedule(&state, req, &session, &stored);
        if upgrade_before_answer {
            return Err(invalid(
                "此候选正在升级核验，请保留当前输入，核验完成后再确认",
            ));
        }
        return Ok(Json(brainstorm_response(
            session_id,
            &session.phase,
            session.revision,
            &stored,
        )));
    }
    if req.action == "abandon" && session.phase == "abandoned" {
        return Ok(Json(brainstorm_response(
            session_id,
            &session.phase,
            session.revision,
            &stored,
        )));
    }
    if req.revision != Some(session.revision) {
        return Err(brainstorm_error(
            StatusCode::CONFLICT,
            "BRAINSTORM_REVISION_CONFLICT",
            "脑暴内容已更新，请刷新后重试",
        ));
    }
    let status = stored
        .stage_workflow
        .as_ref()
        .unwrap()
        .current()
        .status
        .clone();
    let phase = match req.action.as_str() {
        "answer" | "skip" | "exclude" => {
            if status != "answering" {
                return Err(invalid("当前阶段还未准备好回答问题"));
            }
            let question = stored
                .current_question
                .take()
                .ok_or_else(|| invalid("当前没有待回答问题"))?;
            if req.question_id.as_deref() != Some(&question.id) {
                return Err(invalid("当前问题已变化，请刷新后重试"));
            }
            if question.input_clarification.is_some() && req.action != "answer" {
                return Err(invalid("该资料确认题需要由你明确选择或填写，不能跳过"));
            }
            let answer = if req.action == "answer" {
                let answer = req
                    .answer
                    .clone()
                    .ok_or_else(|| invalid("请选择答案或填写补充内容"))?;
                if answer.source != "user" || !validate_brainstorm_answer(&question, &answer) {
                    return Err(invalid("请选择有效答案"));
                }
                answer
            } else {
                BrainstormAnswer {
                    selected_option_ids: vec![],
                    custom_text: if req.action == "exclude" {
                        "该方向不重要"
                    } else {
                        "暂未确定，创作时保留为待核验假设"
                    }
                    .into(),
                    source: if req.action == "exclude" {
                        "user_excluded"
                    } else {
                        "agent_assumption"
                    }
                    .into(),
                }
            };
            let input_kind = question.input_clarification.as_ref()
                .and_then(|marker| marker["kind"].as_str()).map(str::to_owned);
            let input_action = selected_input_action(&question, &answer);
            let confirmed_period = if input_kind.as_deref() == Some("report_period") {
                let timezone = stored.stage_workflow.as_ref().unwrap().current()
                    .input_preflight.as_ref().and_then(|value| value["timezone"].as_str())
                    .unwrap_or("Asia/Shanghai");
                Some(confirmed_answer_period(&question, &answer, timezone)?)
            } else {
                None
            };
            stored
                .user_input_revisions
                .insert(question.id.clone(), session.revision + 1);
            stored.turns.push(BrainstormStoredTurn { question, answer });
            if let Some(period) = confirmed_period {
                let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
                stage.confirmed_period = Some(period);
                stage.input_phase = "pending".into();
                stage.status = "thinking".into();
                stage.generation_id = uuid::Uuid::new_v4().to_string();
                stored.current_question = None;
                "preparing".into()
            } else if input_kind.as_deref() == Some("source_confirmation") {
                if input_action.as_deref() == Some("retry_local_recall")
                    || (input_action.is_none()
                        && stored.turns.last().is_some_and(|turn| !turn.answer.custom_text.trim().is_empty())) {
                    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
                    stage.input_attempt = stage.input_attempt.saturating_add(1);
                    stage.input_phase = "pending".into();
                    stage.status = "thinking".into();
                    stage.generation_id = uuid::Uuid::new_v4().to_string();
                    stored.current_question = None;
                    "preparing".into()
                } else {
                    let all_answered = stored.stage_workflow.as_ref().unwrap().current().questions
                        .iter().filter(|candidate| candidate.input_clarification.is_some())
                        .all(|candidate| stored.turns.iter().any(|turn| turn.question.id == candidate.id));
                    if all_answered {
                        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
                        stage.input_phase = "complete".into();
                        if stage.progress.total_batches == 0 {
                            stage.status = "thinking".into();
                            stored.current_question = None;
                            "preparing".into()
                        } else {
                            select_next(&mut stored)
                        }
                    } else {
                        select_next(&mut stored)
                    }
                }
            } else {
                select_next(&mut stored)
            }
        }
        "edit_brief" => edit_saved_brief(&mut stored, req, session.revision + 1)?,
        "repair_candidate_copy" => repair_candidate_copy(&mut stored, req, session.revision + 1)?,
        "reopen" | "revise_answer" => revise_saved_answer(&mut stored, req, session.revision + 1)?,
        "confirm_stage" => {
            if status != "ready" {
                return Err(invalid("请先完成当前阶段的问题确认"));
            }
            if stored.stage_workflow.as_ref().unwrap().active_stage == "detail"
                && needs_input_preflight(&stored)
                && !detail_input_ready(&stored) {
                if stored.stage_workflow.as_ref().unwrap().current().input_preflight.is_some() {
                    return Err(invalid("请先完成创作细节中的周期与资料确认"));
                }
                let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
                stage.input_phase = "pending".into();
                stage.status = "thinking".into();
                stage.error = None;
                stage.generation_id = uuid::Uuid::new_v4().to_string();
                stored.current_question = None;
                "preparing".into()
            } else {
                let preflight_required = needs_input_preflight(&stored);
                let w = stored.stage_workflow.as_mut().unwrap();
                if w.active_stage == "detail" && w.input_return_stage.as_deref() == Some("writing") {
                    w.current_mut().status = "completed".into();
                    w.input_return_stage = None;
                    w.active_stage = "writing".into();
                    stored.current_question = None;
                    "exploring".into()
                } else {
                    let next = match w.active_stage.as_str() {
                        "direction" => "detail",
                        "detail" => "writing",
                        _ => return Err(invalid("行文阶段请逐章确认并创作")),
                    };
                    w.current_mut().status = "completed".into();
                    w.active_stage = next.into();
                    let current = w.current_mut();
                    current.status = "thinking".into();
                    if next == "detail" && preflight_required {
                        current.input_phase = "pending".into();
                    }
                    current.progress.started_at_ms = now();
                    current.progress.updated_at_ms = now();
                    stored.current_question = None;
                    "preparing".into()
                }
            }
        }
        "review_inputs" => {
            let w = stored.stage_workflow.as_mut().unwrap();
            if session.phase == "completed" || w.active_stage != "writing" || w.stages.iter().find(|s| s.id == "detail")
                .is_none_or(|stage| stage.input_preflight.is_some()) {
                return Err(invalid("当前会话没有待补的创作细节输入"));
            }
            w.input_return_stage = Some("writing".into());
            w.active_stage = "detail".into();
            let stage = w.current_mut();
            stage.input_phase = "pending".into();
            stage.status = "thinking".into();
            stage.error = None;
            stage.generation_id = uuid::Uuid::new_v4().to_string();
            stored.current_question = None;
            "preparing".into()
        }
        "refresh_inputs" => {
            let w = stored.stage_workflow.as_mut().unwrap();
            if w.active_stage != "detail" || w.current().input_preflight.is_none()
                || !matches!(status.as_str(), "answering" | "ready" | "failed") {
                return Err(invalid("当前没有可重检的创作细节资料"));
            }
            let stage = w.current_mut();
            if stage.input_attempt >= 20 {
                return Err(invalid("本轮资料重检次数已达上限，请稍后继续"));
            }
            stage.input_attempt += 1;
            stage.input_phase = "pending".into();
            stage.status = "thinking".into();
            stage.error = None;
            stage.generation_id = uuid::Uuid::new_v4().to_string();
            stored.current_question = None;
            "preparing".into()
        }
        "retry_stage" => {
            refresh_generator_contract(&mut stored)?;
            refresh_catalog_binding(&state, &mut stored)?;
            refresh_root_axes_binding(&mut stored)?;
            if status != "failed" {
                return Err(invalid("当前阶段无需重试"));
            }
            refresh_checkpoint_input(&mut stored, req);
            prepare_pool_review(&mut stored, req)?;
            let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
            if let Some(recovery) = &mut stage.automatic_recovery {
                recovery.step_key.clear();
                recovery.requests_started = 0;
                recovery.stage_retries_used = 0;
                recovery.unavailable_failures = 0;
                recovery.unavailable_retries_used = 0;
                recovery.retry_at_ms = 0;
                recovery.request_in_flight = false;
                recovery.last_failure_code = None;
            }
            stage.generation_id = uuid::Uuid::new_v4().to_string();
            stage.status = "thinking".into();
            stage.error = None;
            stage.progress.updated_at_ms = now();
            "preparing".into()
        }
        "confirm_chapter" => {
            if needs_input_preflight(&stored) && !detail_input_ready(&stored) {
                return Err(invalid("请先在创作细节确认报告周期与资料来源"));
            }
            let w = stored.stage_workflow.as_mut().unwrap();
            let generation = w.current().generation_id.clone();
            let chapter = w
                .chapters
                .iter_mut()
                .find(|c| {
                    Some(&c.id) == w.active_chapter_id.as_ref()
                        && Some(&c.id) == req.chapter_id.as_ref()
                })
                .ok_or_else(|| invalid("请确认当前章节"))?;
            if chapter.status != "ready" && chapter.status != "writing" {
                return Err(invalid("请先回答本章的设计问题"));
            }
            let instruction_id = format!(
                "brainstorm-chapter-{}",
                sha256_hex(&format!("{}:{}:{}", session_id, generation, chapter.id))
            );
            chapter.instruction_id = Some(instruction_id.clone());
            chapter.operation_id = Some(format!(
                "operation-{}",
                sha256_hex(&format!("{}:{}", session_id, instruction_id))
            ));
            chapter.status = "writing".into();
            "chapter_writing".into()
        }
        "chapter_completed" => {
            let w = stored.stage_workflow.as_ref().unwrap();
            let chapter = w
                .chapters
                .iter()
                .find(|c| {
                    Some(&c.id) == w.active_chapter_id.as_ref()
                        && Some(&c.id) == req.chapter_id.as_ref()
                })
                .ok_or_else(|| invalid("请核验当前章节"))?;
            if chapter.status != "writing" {
                return Err(invalid("当前章节还未开始创作"));
            }
            verify_chapter_completion(&state, session_id, chapter)?;
            let w = stored.stage_workflow.as_mut().unwrap();
            let index = w
                .chapters
                .iter()
                .position(|c| Some(&c.id) == w.active_chapter_id.as_ref())
                .unwrap();
            w.chapters[index].status = "completed".into();
            if index + 1 < w.chapters.len() {
                w.active_chapter_id = Some(w.chapters[index + 1].id.clone());
                select_next(&mut stored)
            } else {
                w.current_mut().status = "completed".into();
                w.active_chapter_id = None;
                "completed".into()
            }
        }
        "abandon" => {
            stored.current_question = None;
            "abandoned".into()
        }
        _ => return Err(invalid("三阶段脑暴请使用当前阶段的回答、确认或重试操作")),
    };
    persist(&state, session_id, session.revision, &phase, &stored)?;
    let updated = repo::CreationBrainstormSession {
        revision: session.revision + 1,
        phase: phase.clone(),
        ..session
    };
    schedule(&state, req, &updated, &stored);
    Ok(Json(brainstorm_response(
        session_id,
        &phase,
        updated.revision,
        &stored,
    )))
}

fn schedule(
    state: &Arc<AppState>,
    req: &BrainstormTurnRequest,
    session: &repo::CreationBrainstormSession,
    stored: &BrainstormStoredState,
) {
    let stage = stored.stage_workflow.as_ref().unwrap().current();
    let key = format!("{}:{}", req.session_id.trim(), stage.generation_id);
    let prefix = format!("{}:", req.session_id.trim());
    // Validate the snapshot before *any* cache mutation. A delayed status read
    // from a previous stage must neither start old work nor abort the new job.
    // Storage -> jobs is also the lock order used by dispatch and persistence.
    let _ = with_brainstorm_session_guard(
        state,
        req.session_id.trim(),
        session.phase == "abandoned",
        |conn| {
            let latest = repo::get(conn, req.session_id.trim())?;
            if !latest.is_some_and(|s| s.revision == session.revision && s.phase == session.phase) {
                return Ok(());
            }
            let mut jobs = state.brainstorm_stage_jobs.lock().unwrap();
            jobs.retain(|id, task| {
                if id.starts_with(&prefix)
                    && (session.phase == "abandoned" || id != &key || stage.status != "thinking")
                {
                    task.abort();
                    false
                } else {
                    !task.is_finished()
                }
            });
            if session.phase == "abandoned" || stage.status != "thinking" || jobs.contains_key(&key)
            {
                return Ok(());
            }
            let state = state.clone();
            let req = req.clone();
            let key_for_task = key.clone();
            let generation = stage.generation_id.clone();
            jobs.insert(
                key,
                tokio::spawn(async move {
                    run_batches(&state, &req, &generation).await;
                    state
                        .brainstorm_stage_jobs
                        .lock()
                        .unwrap()
                        .remove(&key_for_task);
                }),
            );
            Ok(())
        },
    );
}

async fn request_batch(
    state: &AppState,
    req: &BrainstormTurnRequest,
    stored: &BrainstormStoredState,
) -> Result<serde_json::Value, Failure> {
    let payload = batch_payload(req, stored)?;
    let configured_timeout =
        std::env::var("MEMORYBREAD_BRAINSTORM_STAGE_BATCH_TIMEOUT_SECONDS").ok();
    let response = creation_sidecar_client()
        .post(format!(
            "{}/creation/brainstorm/stage/batch",
            state.creation_sidecar_url.trim_end_matches('/')
        ))
        .timeout(stage_batch_http_timeout(configured_timeout.as_deref()))
        .json(&payload)
        .send()
        .await
        .map_err(|e| {
            brainstorm_model_error(if e.is_timeout() {
                "BRAINSTORM_MODEL_TIMEOUT"
            } else {
                "BRAINSTORM_MODEL_UNAVAILABLE"
            })
        })?;
    if !response.status().is_success() {
        let status =
            StatusCode::from_u16(response.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
        let body: serde_json::Value = response.json().await.unwrap_or_default();
        // Status boundaries survive normalization: malformed input, revoked
        // authority and explicit cancellation cannot become retryable output.
        if matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) {
            return Err(brainstorm_model_error("MODEL_ACCESS_DENIED"));
        }
        if matches!(
            status,
            StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY
        ) {
            return Err(brainstorm_model_error("MODEL_REQUEST_FAILED"));
        }
        if status == StatusCode::CONFLICT || status.as_u16() == 499 {
            let mut failure = invalid("阶段准备已停止或输入已更新，请检查当前配置后继续");
            failure.1 .0["retryable"] = serde_json::json!(false);
            return Err(failure);
        }
        let failure = brainstorm_sidecar_error(status, &body);
        let code = body
            .get("detail")
            .unwrap_or(&body)
            .get("code")
            .and_then(serde_json::Value::as_str);
        if failure.1 .0["code"] == "BRAINSTORM_MODEL_OUTPUT_INVALID"
            && code != Some("BRAINSTORM_MODEL_OUTPUT_INVALID")
        {
            if matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) {
                return Err(brainstorm_model_error("MODEL_ACCESS_DENIED"));
            }
            if matches!(
                status,
                StatusCode::BAD_REQUEST | StatusCode::UNPROCESSABLE_ENTITY
            ) {
                return Err(brainstorm_model_error("MODEL_REQUEST_FAILED"));
            }
        }
        return Err(failure);
    }
    response
        .json()
        .await
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))
}

fn input_fingerprint(req: &BrainstormTurnRequest, stored: &BrainstormStoredState) -> String {
    let workflow = stored.stage_workflow.as_ref().unwrap();
    let stage = workflow.current();
    // Session revision and worker generation change after every save/retry.
    // Only authoritative input chronology belongs in this stable identity.
    let mut inputs = serde_json::json!({
        "session_id": req.session_id.trim(), "stage": workflow.active_stage,
        "preparation_contract_version": if stage.content_mode {
            CONTENT_PREPARATION_CONTRACT_VERSION
        } else if legacy_root_checkpoint_allowed(stored, stage.preparation_contract_version) {
            stage.preparation_contract_version
        } else { PREPARATION_CONTRACT_VERSION },
        "root_request": stored.root_request, "decisions": brainstorm_decision_context(stored),
        "brief_edits": stored.brief_edits, "user_input_revisions": stored.user_input_revisions,
        "selected_skills": stored.selected_skills, "limits": stage_limits(),
        "catalog_binding": workflow.current().catalog_binding,
    });
    if stage.content_mode {
        inputs["content_mode"] = serde_json::json!(true);
        if let Some(period) = &stage.confirmed_period {
            inputs["confirmed_period"] = period.clone();
        }
        if let Some(preflight) = &stage.input_preflight {
            inputs["input_preflight"] = preflight.clone();
        }
    }
    if let Some(binding) = &workflow.current().root_axes_binding {
        inputs["root_axes_binding"] = serde_json::to_value(binding).unwrap();
    }
    // Version14 already included the compiled manifest in its input identity.
    if workflow.current().preparation_contract_version >= 14
        && workflow
            .current()
            .topics
            .iter()
            .any(|topic| !topic.root_axis_ids.is_empty())
    {
        inputs["compiled_topic_manifest"] =
            serde_json::to_value(&workflow.current().topics).unwrap();
    }
    // Keep old sessions' identity unchanged. Style preferences never alter planning facts.
    if let Some(facts) = brainstorm_work_facts(stored) {
        inputs
            .as_object_mut()
            .unwrap()
            .insert("work_facts".into(), facts);
    }
    sha256_hex(&inputs.to_string())
}

fn current_topic(stored: &BrainstormStoredState) -> Result<&Topic, Failure> {
    let stage = stored.stage_workflow.as_ref().unwrap().current();
    stage
        .progress
        .completed_batches
        .checked_sub(1)
        .and_then(|index| stage.topics.get(index))
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))
}

fn archive_topic_checkpoint(stage: &mut Stage, reason: &str) {
    if let Some(checkpoint) = stage.topic_checkpoint.take() {
        stage
            .topic_checkpoint_archives
            .push(TopicCheckpointArchive {
                reason: reason.into(),
                archived_at_ms: now(),
                checkpoint,
            });
    }
    stage.preparation_progress = None;
}

fn refresh_checkpoint_input(
    stored: &mut BrainstormStoredState,
    req: &BrainstormTurnRequest,
) -> bool {
    let fingerprint = input_fingerprint(req, stored);
    let allow_legacy = legacy_root_checkpoint_allowed(
        stored,
        stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .preparation_contract_version,
    );
    let topic = current_topic(stored).ok().map(|topic| topic.id.clone());
    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
    let reason = stage.topic_checkpoint.as_ref().and_then(|checkpoint| {
        if checkpoint.preparation_contract_version != PREPARATION_CONTRACT_VERSION
            && !(allow_legacy
                && (12..PREPARATION_CONTRACT_VERSION)
                    .contains(&checkpoint.preparation_contract_version))
        {
            Some("contract_changed")
        } else if Some(&checkpoint.topic_id) != topic.as_ref()
            || checkpoint.input_fingerprint != fingerprint
            || checkpoint.limits != BuildLimits::configured()
        {
            Some("input_changed")
        } else {
            None
        }
    });
    if let Some(reason) = reason {
        archive_topic_checkpoint(stage, reason);
        true
    } else {
        false
    }
}

fn pool_fingerprint(stage: &Stage) -> String {
    sha256_hex(&serde_json::json!({"questions":stage.questions,"topics":stage.topics}).to_string())
}

fn catalog_invalid() -> Failure {
    invalid("当前技能脑暴配置已失效，请更新技能后重试")
}

fn root_axes_invalid() -> Failure {
    invalid("当前脑暴能力配置已变化或失效，请保留进度后重试")
}

fn root_axes_refresh_allowed(stored: &BrainstormStoredState) -> bool {
    let workflow = stored.stage_workflow.as_ref().unwrap();
    let stage = workflow.current();
    !stage.content_mode
        && matches!(workflow.active_stage.as_str(), "direction" | "detail")
        && stage.catalog_binding.is_none()
        && matches!(
            stage.status.as_str(),
            "thinking" | "failed" | "answering" | "ready"
        )
        && review_is_allowed(stored)
        && !workflow.copy_repair_history.iter().any(|repair| {
            stage
                .questions
                .iter()
                .any(|question| question.id == repair.question_id)
        })
}

fn legacy_root_checkpoint_allowed(stored: &BrainstormStoredState, version: u32) -> bool {
    let workflow = stored.stage_workflow.as_ref().unwrap();
    (12..PREPARATION_CONTRACT_VERSION).contains(&version)
        && matches!(workflow.active_stage.as_str(), "direction" | "detail")
        && workflow.current().catalog_binding.is_none()
        && !root_axes_refresh_allowed(stored)
}

fn refresh_root_axes_binding(stored: &mut BrainstormStoredState) -> Result<bool, Failure> {
    if !root_axes_refresh_allowed(stored) {
        return Ok(false);
    }
    let binding = root_axes::load().map_err(|_| root_axes_invalid())?;
    let stage = stored.stage_workflow.as_ref().unwrap().current();
    if stage.root_axes_binding.as_ref() == Some(&binding) {
        return Ok(false);
    }
    // A registry drift invalidates the complete unpaid plan, including topics
    // already generated under its old declared attribute capabilities.
    if !stage.topics.is_empty() || !stage.questions.is_empty() || stage.topic_checkpoint.is_some() {
        archive_topic_checkpoint(
            stored.stage_workflow.as_mut().unwrap().current_mut(),
            "root_axes_changed",
        );
        archive_preparation_pool(stored, "root_axes_changed", vec![], vec![])?;
    }
    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
    stage.root_axes_binding = Some(binding);
    stage.topics.clear();
    stage.progress.completed_batches = 0;
    stage.progress.total_batches = 0;
    stage.progress.percent = 0;
    stage.progress.estimated_remaining_seconds = None;
    stage.status = "thinking".into();
    stage.error = None;
    stage.automatic_recovery = None;
    stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
    stored.current_question = None;
    Ok(true)
}

fn refresh_generator_contract(stored: &mut BrainstormStoredState) -> Result<bool, Failure> {
    let stage = stored.stage_workflow.as_ref().unwrap().current();
    if stage.content_mode
        || !(7..PREPARATION_CONTRACT_VERSION).contains(&stage.preparation_contract_version)
        || stage.catalog_binding.is_some()
        || !matches!(
            stage.status.as_str(),
            "thinking" | "failed" | "answering" | "ready"
        )
        || !review_is_allowed(stored)
        || stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .copy_repair_history
            .iter()
            .any(|repair| {
                stage
                    .questions
                    .iter()
                    .any(|question| question.id == repair.question_id)
            })
    {
        return Ok(false);
    }
    // Earlier model manifests and checkpoints may reopen confirmed choices,
    // contain incomplete goals/scopes or promote historical facts into authority.
    // Keep the complete old plan as evidence; no legacy goal seeds the new job.
    archive_topic_checkpoint(
        stored.stage_workflow.as_mut().unwrap().current_mut(),
        "contract_changed",
    );
    archive_preparation_pool(stored, "generator_contract_changed", vec![], vec![])?;
    let workflow = stored.stage_workflow.as_mut().unwrap();
    let stage = workflow.current_mut();
    stage.topics.clear();
    stage.progress.completed_batches = 0;
    stage.progress.total_batches = 0;
    stage.progress.percent = 0;
    stage.status = "thinking".into();
    stage.error = None;
    stage.automatic_recovery = None;
    if workflow.active_stage == "writing" {
        workflow.chapters.clear();
        workflow.active_chapter_id = None;
    }
    Ok(true)
}

fn refresh_catalog_binding(
    state: &AppState,
    stored: &mut BrainstormStoredState,
) -> Result<bool, Failure> {
    // A catalog upgrade cannot replace answers, manual edits or protected work.
    if !review_is_allowed(stored) {
        return Ok(false);
    }
    let workflow = stored.stage_workflow.as_ref().unwrap();
    let stage_name = workflow.active_stage.clone();
    if workflow.current().content_mode {
        return Ok(false);
    }
    let limits = BuildLimits::configured();
    let binding = brainstorm_catalog::resolve(
        state,
        &stored.selected_skills,
        &stage_name,
        stage_limits()["max_topics"].as_u64().unwrap() as usize,
        limits.max_questions_per_topic,
        limits.max_depth,
    )
    .map_err(|_| catalog_invalid())?;
    let stage = workflow.current();
    if binding.is_none() {
        if stage.catalog_binding.is_some() {
            return Err(catalog_invalid());
        }
        return Ok(false);
    }
    if stage.catalog_binding == binding
        && stage.preparation_contract_version == PREPARATION_CONTRACT_VERSION
    {
        return Ok(false);
    }
    let reason = if stage
        .topic_checkpoint
        .as_ref()
        .is_some_and(|cp| cp.preparation_contract_version != PREPARATION_CONTRACT_VERSION)
    {
        "contract_changed"
    } else {
        "input_changed"
    };
    archive_topic_checkpoint(
        stored.stage_workflow.as_mut().unwrap().current_mut(),
        reason,
    );
    if !stored
        .stage_workflow
        .as_ref()
        .unwrap()
        .current()
        .topics
        .is_empty()
        || !stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .questions
            .is_empty()
    {
        archive_preparation_pool(stored, "catalog_changed", vec![], vec![])?;
    }
    let workflow = stored.stage_workflow.as_mut().unwrap();
    let stage = workflow.current_mut();
    stage.topics.clear();
    stage.questions.clear();
    stage.review_checkpoint = None;
    stage.catalog_binding = binding;
    stage.root_axes_binding = None;
    stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
    stage.progress.completed_batches = 0;
    stage.progress.total_batches = 0;
    stage.progress.percent = 0;
    stage.progress.estimated_remaining_seconds = None;
    if stage_name == "writing" {
        workflow.chapters.clear();
        workflow.active_chapter_id = None;
    }
    stored.current_question = None;
    Ok(true)
}

fn review_is_allowed(stored: &BrainstormStoredState) -> bool {
    let workflow = stored.stage_workflow.as_ref().unwrap();
    let stage = workflow.current();
    !has_protected_chapters(workflow)
        && !workflow
            .chapters
            .iter()
            .any(|chapter| chapter.instruction_id.is_some() || chapter.operation_id.is_some())
        && !stored.turns.iter().any(|turn| {
            stage
                .questions
                .iter()
                .any(|question| question.id == turn.question.id)
        })
        && !stage
            .questions
            .iter()
            .any(|question| stored.brief_edits.contains_key(&question.id))
}

fn review_question_topic<'a>(stage: &'a Stage, question: &BrainstormQuestion) -> Option<&'a Topic> {
    let suffix = question.id.strip_prefix("stage-")?;
    uuid::Uuid::parse_str(suffix.get(..36)?).ok()?;
    if suffix.as_bytes().get(36) != Some(&b'-') {
        return None;
    }
    let (batch, ordinal) = suffix.get(37..)?.split_once('-')?;
    if ordinal.is_empty() || !ordinal.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    stage
        .topics
        .get(batch.parse::<usize>().ok()?.checked_sub(1)?)
}

fn archive_preparation_pool(
    stored: &mut BrainstormStoredState,
    reason: &str,
    codes: Vec<String>,
    decision_ids: Vec<String>,
) -> Result<(), Failure> {
    if !review_is_allowed(stored) {
        return Err(invalid("已有确认或章节内容，请保留当前候选并回看后再调整"));
    }
    let workflow = stored.stage_workflow.as_mut().unwrap();
    let archived_chapters = if workflow.active_stage == "writing" {
        workflow.chapters.clone()
    } else {
        vec![]
    };
    let stage = workflow.current_mut();
    let revoke_topics = codes.iter().any(|code| code == "source_permission_revoked");
    stage.preparation_archives.push(PreparationArchive {
        reason: reason.into(),
        archived_at_ms: now(),
        preparation_contract_version: stage.preparation_contract_version,
        generation_id: stage
            .review_checkpoint
            .as_ref()
            .filter(|checkpoint| !checkpoint.original_generation_id.is_empty())
            .map(|checkpoint| checkpoint.original_generation_id.clone())
            .unwrap_or_else(|| stage.generation_id.clone()),
        status: stage
            .review_checkpoint
            .as_ref()
            .filter(|checkpoint| !checkpoint.original_status.is_empty())
            .map(|checkpoint| checkpoint.original_status.clone())
            .unwrap_or_else(|| stage.status.clone()),
        questions: stage.questions.clone(),
        topics: stage.topics.clone(),
        progress: stage
            .review_checkpoint
            .as_ref()
            .and_then(|checkpoint| checkpoint.original_progress.clone())
            .unwrap_or_else(|| stage.progress.clone()),
        codes,
        decision_ids,
        catalog_binding: stage.catalog_binding.clone(),
        root_axes_binding: stage.root_axes_binding.clone(),
        chapters: archived_chapters,
        error: stage.error.clone(),
        automatic_recovery: stage.automatic_recovery.clone(),
    });
    archive_topic_checkpoint(stage, "input_changed");
    stage.questions.clear();
    if revoke_topics {
        stage.topics.clear();
    }
    stage.review_checkpoint = None;
    stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
    stage.progress.completed_batches = usize::from(!stage.topics.is_empty());
    stage.progress.total_batches = if stage.topics.is_empty() {
        0
    } else {
        stage.topics.len() + 1
    };
    stage.progress.percent =
        stage.progress.completed_batches * 100 / stage.progress.total_batches.max(1);
    stage.progress.started_at_ms = now();
    stage.progress.updated_at_ms = now();
    stage.progress.estimated_remaining_seconds = None;
    stored.current_question = None;
    if workflow.active_stage == "writing" {
        if revoke_topics {
            workflow.chapters.clear();
        }
        for chapter in &mut workflow.chapters {
            chapter.question_ids.clear();
            chapter.status = "pending".into();
        }
        workflow.active_chapter_id = None;
    }
    Ok(())
}

fn prepare_pool_review(
    stored: &mut BrainstormStoredState,
    req: &BrainstormTurnRequest,
) -> Result<bool, Failure> {
    let stage = stored.stage_workflow.as_ref().unwrap().current();
    if stage.content_mode
        || stage.preparation_contract_version == PREPARATION_CONTRACT_VERSION
        || !matches!(
            stage.status.as_str(),
            "thinking" | "failed" | "answering" | "ready"
        )
    {
        return Ok(false);
    }
    if stage.questions.is_empty() {
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
        stage.review_checkpoint = None;
        return Ok(true);
    }
    if !review_is_allowed(stored) {
        return Ok(false);
    }
    if stage
        .questions
        .iter()
        .any(|question| review_question_topic(stage, question).is_none())
    {
        archive_preparation_pool(stored, "unsupported_legacy_binding", vec![], vec![])?;
        stored.stage_workflow.as_mut().unwrap().current_mut().status = "thinking".into();
        return Ok(true);
    }
    let input = input_fingerprint(req, stored);
    let pool = pool_fingerprint(stage);
    let original_status = stage
        .review_checkpoint
        .as_ref()
        .filter(|checkpoint| !checkpoint.original_status.is_empty())
        .map(|checkpoint| checkpoint.original_status.clone())
        .unwrap_or_else(|| stage.status.clone());
    let original_generation_id = stage
        .review_checkpoint
        .as_ref()
        .filter(|checkpoint| !checkpoint.original_generation_id.is_empty())
        .map(|checkpoint| checkpoint.original_generation_id.clone())
        .unwrap_or_else(|| stage.generation_id.clone());
    let original_progress = stage
        .review_checkpoint
        .as_ref()
        .and_then(|checkpoint| checkpoint.original_progress.clone())
        .unwrap_or_else(|| stage.progress.clone());
    if stage.review_checkpoint.as_ref().is_some_and(|checkpoint| {
        checkpoint.preparation_contract_version == PREPARATION_CONTRACT_VERSION
            && checkpoint.input_fingerprint == input
            && checkpoint.pool_fingerprint == pool
            && checkpoint.next_index < stage.questions.len()
    }) {
        return Ok(false);
    }
    stored
        .stage_workflow
        .as_mut()
        .unwrap()
        .current_mut()
        .review_checkpoint = Some(PreparationReviewCheckpoint {
        preparation_contract_version: PREPARATION_CONTRACT_VERSION,
        input_fingerprint: input,
        pool_fingerprint: pool,
        next_index: 0,
        unit_budget: review_unit_budget(
            std::env::var("MEMORYBREAD_BRAINSTORM_STAGE_REVIEW_UNIT_BUDGET")
                .ok()
                .as_deref(),
        ),
        original_status,
        original_generation_id,
        original_progress: Some(original_progress),
    });
    if matches!(
        stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .status
            .as_str(),
        "answering" | "ready"
    ) {
        stored.stage_workflow.as_mut().unwrap().current_mut().status = "thinking".into();
        stored.current_question = None;
    }
    Ok(true)
}

fn question_ancestor_conditions(
    question: &BrainstormQuestion,
    stage: &Stage,
) -> Result<Vec<serde_json::Value>, Failure> {
    let mut result = vec![];
    let mut parent_id = question.parent_question_id.as_ref();
    let mut option_id = question.parent_option_id.as_ref();
    let mut seen = HashSet::new();
    while let Some(id) = parent_id {
        if !seen.insert(id) {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        let parent = stage
            .questions
            .iter()
            .find(|question| &question.id == id)
            .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
        if review_question_topic(stage, parent).map(|topic| &topic.id)
            != review_question_topic(stage, question).map(|topic| &topic.id)
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        let option = parent
            .options
            .iter()
            .find(|option| Some(&option.id) == option_id)
            .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
        result.push(serde_json::json!({"parent_question_id":parent.id,"parent_option_id":option.id,"parent_prompt":parent.prompt,"option_label":option.label,"option_description":option.description}));
        parent_id = parent.parent_question_id.as_ref();
        option_id = parent.parent_option_id.as_ref();
    }
    result.reverse();
    Ok(result)
}

fn validate_rendered_root_question(
    binding: &RootAxesBinding,
    stage: &str,
    axis_id: &str,
    topic: &Topic,
    selection: &serde_json::Value,
    question: &BrainstormQuestion,
) -> Result<(), Failure> {
    let expected = binding
        .render_values(stage, axis_id, selection, 5)
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let mut options = expected["options"]
        .as_array()
        .ok_or_else(root_axes_invalid)?
        .iter()
        .enumerate()
        .collect::<Vec<_>>();
    // Recommendation changes presentation order only; original provider order
    // owns option identity and its exact finite value/source selection.
    options.sort_by_key(|(_, option)| !option["recommended"].as_bool().unwrap_or(false));
    if question.question_type != expected["type"].as_str().unwrap_or_default()
        || question.prompt != expected["prompt_body"].as_str().unwrap_or_default()
        || question.why_now != expected["why_now"].as_str().unwrap_or_default()
        || question.single_choice_reason
            != expected["single_choice_reason"]
                .as_str()
                .unwrap_or_default()
        || question.answer_template != expected["answer_template"].as_str().unwrap_or_default()
        || question.dimension_id != topic.id
        || question.dimension != topic.title
        || !question.required
        || !question.allow_custom
        || question.parent_question_id.is_some()
        || question.parent_option_id.is_some()
        || question.options.len() != options.len()
        || question
            .options
            .iter()
            .zip(options)
            .any(|(actual, (ordinal, expected))| {
                actual.id != ((b'a' + ordinal as u8) as char).to_string()
                    || actual.label != expected["label"].as_str().unwrap_or_default()
                    || actual.description != expected["description"].as_str().unwrap_or_default()
                    || actual.recommended != expected["recommended"].as_bool().unwrap_or(false)
            })
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    // Citation details are kept by the existing Sidecar source/fact gates.
    // This proof checks finite copy, not the truth of a retrieved source.
    Ok(())
}

fn verified_atomic_checkpoint(checkpoint: &TopicCheckpoint, stage: &str) -> Result<bool, Failure> {
    // Version15 is atomic already. A protected old atomic checkpoint must never
    // fall back to generic tree mode merely because a newer renderer exists.
    // Its old snapshot cannot authorize the current immutable value catalog.
    if checkpoint.preparation_contract_version == 15
        && checkpoint.catalog_binding.is_none()
        && matches!(stage, "direction" | "detail")
    {
        return Err(root_axes_invalid());
    }
    let expected = checkpoint.preparation_contract_version == PREPARATION_CONTRACT_VERSION
        && checkpoint.catalog_binding.is_none()
        && matches!(stage, "direction" | "detail");
    if checkpoint.atomic_root_questions != expected {
        return Err(root_axes_invalid());
    }
    if !expected {
        return Ok(false);
    }
    let binding = checkpoint
        .root_axes_binding
        .as_ref()
        .ok_or_else(root_axes_invalid)?;
    let axes = binding.eligible(stage).map_err(|_| root_axes_invalid())?;
    if checkpoint.root_axis_ids.is_empty()
        || checkpoint.root_axis_ids != checkpoint.topic.root_axis_ids
        || checkpoint.next_node_index != checkpoint.root_axis_ids.len()
        || checkpoint.questions.len() + checkpoint.frontier.len() != checkpoint.root_axis_ids.len()
        || checkpoint.root_axis_ids.len() > checkpoint.limits.max_questions_per_topic
        || !(2..=3).contains(&checkpoint.limits.max_depth)
        || !(3..=24).contains(&checkpoint.limits.max_questions_per_topic)
        || checkpoint.root_value_selections.len() != checkpoint.questions.len()
    {
        return Err(root_axes_invalid());
    }
    let roots = checkpoint
        .root_axis_ids
        .iter()
        .map(|id| {
            axes.iter()
                .find(|axis| axis["id"] == *id)
                .and_then(|axis| axis["goal"].as_str())
                .map(str::to_owned)
                .ok_or_else(root_axes_invalid)
        })
        .collect::<Result<Vec<_>, _>>()?;
    binding
        .validate_atomic_goals(stage, &roots)
        .map_err(|_| root_axes_invalid())?;
    if binding
        .validate_goals(stage, &roots)
        .map_err(|_| root_axes_invalid())?
        != checkpoint.root_axis_ids
    {
        return Err(root_axes_invalid());
    }
    let ordinal = |id: &str| -> Option<usize> {
        let tail = id.strip_prefix(&format!("stage-{}-", checkpoint.build_id))?;
        let (batch, index) = tail.split_once('-')?;
        if batch.parse::<usize>().ok()? == 0 {
            return None;
        }
        index.parse().ok()
    };
    if checkpoint.questions.iter().enumerate().any(|(index, q)| {
        ordinal(&q.id) != Some(index)
            || q.parent_question_id.is_some()
            || q.parent_option_id.is_some()
    }) || checkpoint.frontier.iter().enumerate().any(|(index, node)| {
        let index = index + checkpoint.questions.len();
        ordinal(&node.id) != Some(index)
            || node.depth != 1
            || node.parent_question_id.is_some()
            || node.parent_option_id.is_some()
            || roots.get(index) != Some(&node.goal)
    }) {
        return Err(root_axes_invalid());
    }
    for (index, (question, proof)) in checkpoint
        .questions
        .iter()
        .zip(&checkpoint.root_value_selections)
        .enumerate()
    {
        if proof.node_id != question.id {
            return Err(root_axes_invalid());
        }
        validate_rendered_root_question(
            binding,
            stage,
            &checkpoint.root_axis_ids[index],
            &checkpoint.topic,
            &proof.selection,
            question,
        )?;
    }
    Ok(true)
}

fn root_limit_for_stage(stage: &Stage, stage_name: &str, limits: &BuildLimits) -> usize {
    if stage.preparation_contract_version == PREPARATION_CONTRACT_VERSION
        && stage.catalog_binding.is_none()
        && matches!(stage_name, "direction" | "detail")
    {
        limits.max_questions_per_topic
    } else {
        limits.max_questions_per_topic / limits.minimum_nodes(1)
    }
}

fn node_budget(
    checkpoint: &TopicCheckpoint,
    node: &NodeGoal,
    stage: &str,
) -> Result<(usize, usize), Failure> {
    if verified_atomic_checkpoint(checkpoint, stage)? {
        if node.depth != 1 || node.parent_question_id.is_some() || node.parent_option_id.is_some() {
            return Err(root_axes_invalid());
        }
        return Ok((5, 0));
    }
    if let Some(binding) = &checkpoint.catalog_binding {
        let stage_name = checkpoint
            .catalog_stage
            .as_deref()
            .ok_or_else(catalog_invalid)?;
        let slot = binding
            .slot(stage_name, &node.goal)
            .ok_or_else(catalog_invalid)?;
        let minimum = checkpoint
            .frontier
            .iter()
            .try_fold(checkpoint.questions.len(), |count, goal| {
                count.checked_add(binding.subtree_size(stage_name, &goal.goal)?)
            })
            .ok_or_else(catalog_invalid)?;
        if minimum > checkpoint.limits.max_questions_per_topic {
            return Err(catalog_invalid());
        }
        return Ok((
            slot.methods.len(),
            slot.methods
                .iter()
                .map(|method| method.child_slot_ids.len())
                .sum(),
        ));
    }
    let reserved = checkpoint
        .frontier
        .iter()
        .skip(1)
        .try_fold(0usize, |total, goal| {
            total.checked_add(checkpoint.limits.minimum_nodes(goal.depth))
        })
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let available = checkpoint
        .limits
        .max_questions_per_topic
        .checked_sub(checkpoint.questions.len() + 1 + reserved)
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if node.depth == checkpoint.limits.max_depth {
        return Ok((5, 0));
    }
    let goals = available / checkpoint.limits.minimum_nodes(node.depth + 1);
    if goals < 2 {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    Ok((goals.min(5), goals))
}

fn prepared_question_prefix(
    question: &BrainstormQuestion,
    saved_questions: &[&BrainstormQuestion],
) -> Option<String> {
    let mut labels = vec![];
    let mut parent_id = question.parent_question_id.as_ref();
    let mut option_id = question.parent_option_id.as_ref();
    let mut seen = HashSet::new();
    while let Some(id) = parent_id {
        if !seen.insert(id) {
            return None;
        }
        let Some(parent) = saved_questions.iter().find(|parent| &parent.id == id) else {
            return None;
        };
        let Some(option) = parent
            .options
            .iter()
            .find(|option| Some(&option.id) == option_id)
        else {
            return None;
        };
        labels.push(option.label.as_str());
        parent_id = parent.parent_question_id.as_ref();
        option_id = parent.parent_option_id.as_ref();
    }
    if labels.is_empty() {
        return Some(String::new());
    }
    labels.reverse();
    Some(format!(
        "若{}，",
        labels
            .iter()
            .map(|label| format!("「{label}」"))
            .collect::<Vec<_>>()
            .join("且")
    ))
}

fn prepared_question_body<'a>(
    question: &'a BrainstormQuestion,
    saved_questions: &[&BrainstormQuestion],
) -> &'a str {
    prepared_question_prefix(question, saved_questions)
        .and_then(|prefix| question.prompt.strip_prefix(&prefix))
        .unwrap_or(&question.prompt)
}

fn batch_payload(
    req: &BrainstormTurnRequest,
    stored: &BrainstormStoredState,
) -> Result<serde_json::Value, Failure> {
    let w = stored.stage_workflow.as_ref().unwrap();
    let stage = w.current();
    if w.active_stage == "detail" && !stage.input_phase.is_empty() && stage.input_phase != "complete" {
        let input_decisions = stored.turns.iter().filter_map(|turn| {
            let marker = turn.question.input_clarification.as_ref()?;
            if turn.answer.source != "user" {
                return None;
            }
            let selected_options = turn.answer.selected_option_ids.iter().filter_map(|id| {
                turn.question.options.iter().find(|option| &option.id == id)
                    .map(|option| serde_json::json!({"id":option.id,"input_value":option.input_value}))
            }).collect::<Vec<_>>();
            Some(serde_json::json!({
                "question_id": turn.question.id,
                "input_clarification": marker,
                "answer_source": turn.answer.source,
                "selected_options": selected_options,
                "custom_text": turn.answer.custom_text,
            }))
        }).collect::<Vec<_>>();
        return Ok(serde_json::json!({
            "stage":"detail", "kind":"input_preflight",
            "content_mode":stage.content_mode,
            "root_request":stored.root_request,
            "decisions":brainstorm_decision_context(stored),
            "selected_skills":stored.selected_skills,
            "brief_edits":stored.brief_edits,
            "user_input_revisions":stored.user_input_revisions,
            "context_id":input_fingerprint(req, stored),
            "confirmed_period":stage.confirmed_period,
            "input_decisions":input_decisions,
            "input_as_of_local_date":stage.input_preflight.as_ref().and_then(|value| value.get("as_of_local_date")),
            "input_timezone":stage.input_preflight.as_ref().and_then(|value| value.get("timezone")),
            "input_attempt":stage.input_attempt,
            "creation_model":req.creation_model,
            "creation_api_key":req.creation_api_key,
            "creation_base_url":req.creation_base_url,
        }));
    }
    if stage.content_mode {
        if stage.preparation_contract_version != CONTENT_PREPARATION_CONTRACT_VERSION
            || stage.catalog_binding.is_some()
            || stage.root_axes_binding.is_some()
            || stage.progress.completed_batches != 0
            || stage.progress.total_batches > 1
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        return Ok(serde_json::json!({
            "stage": w.active_stage,
            "kind": "content_questions",
            "root_request": stored.root_request,
            "decisions": brainstorm_decision_context(stored),
            "brief_markdown": brainstorm_brief(stored),
            "brief_edits": stored.brief_edits,
            "user_input_revisions": stored.user_input_revisions,
            "selected_skills": stored.selected_skills,
            "context_id": input_fingerprint(req, stored),
            "confirmed_period": stage.confirmed_period,
            "plan": {"input_preflight": stage.input_preflight},
            "creation_model": req.creation_model,
            "creation_api_key": req.creation_api_key,
            "creation_base_url": req.creation_base_url,
        }));
    }
    let plan = stage.progress.completed_batches == 0;
    let topic = if plan {
        None
    } else {
        stage.topics.get(stage.progress.completed_batches - 1)
    };
    let checkpoint = stage.topic_checkpoint.as_ref();
    let prepared_questions: Vec<_> = stage
        .questions
        .iter()
        .chain(checkpoint.into_iter().flat_map(|cp| cp.questions.iter()))
        .collect();
    let node = if let Some(checkpoint) = checkpoint {
        let goal = checkpoint
            .frontier
            .first()
            .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
        let (max_options, max_next_goals) = node_budget(checkpoint, goal, &w.active_stage)?;
        let mut ancestors = vec![];
        let mut parent_id = goal.parent_question_id.as_ref();
        let mut option_id = goal.parent_option_id.as_ref();
        let mut seen = HashSet::new();
        while let Some(id) = parent_id {
            if !seen.insert(id) {
                return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
            }
            let parent = checkpoint
                .questions
                .iter()
                .find(|question| &question.id == id)
                .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
            let option = parent
                .options
                .iter()
                .find(|option| Some(&option.id) == option_id)
                .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
            ancestors.push(serde_json::json!({"parent_question_id": parent.id,
                "parent_option_id": option.id, "parent_prompt": parent.prompt,
                "option_label": option.label, "option_description": option.description}));
            parent_id = parent.parent_question_id.as_ref();
            option_id = parent.parent_option_id.as_ref();
        }
        ancestors.reverse();
        Some(
            serde_json::json!({"id": goal.id, "goal": goal.goal, "depth": goal.depth,
            "parent_question_id": goal.parent_question_id, "parent_option_id": goal.parent_option_id,
            "ancestor_conditions": ancestors, "max_options": max_options, "max_next_goals": max_next_goals}),
        )
    } else {
        None
    };
    let limits = BuildLimits::configured();
    if stage.catalog_binding.is_none()
        && !plan
        && node.is_none()
        && root_limit_for_stage(stage, &w.active_stage, &limits) == 0
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let mut payload = serde_json::json!({
        "stage": w.active_stage, "kind": if plan {"plan"} else if node.is_some() {"node"} else {"topic_plan"},
        "root_request": stored.root_request, "decisions": brainstorm_decision_context(stored),
        "brief_markdown": brainstorm_brief(stored), "selected_skills": stored.selected_skills,
        "brief_edits": stored.brief_edits, "user_input_revisions": stored.user_input_revisions,
        "topic": topic, "limits": stage_limits(), "context_id": input_fingerprint(req, stored),
        "root_limit": root_limit_for_stage(stage, &w.active_stage, &limits), "node": node,
        "plan": {"topics": stage.topics,
            "prepared_question_count": prepared_questions.len(),
            "prepared_question_prompts": prepared_questions.iter().map(|q| q.prompt.as_str()).collect::<Vec<_>>(),
            "prepared_question_bodies": prepared_questions.iter().map(|q| prepared_question_body(q, &prepared_questions)).collect::<Vec<_>>()},
        "creation_model": req.creation_model, "creation_api_key": req.creation_api_key,
        "creation_base_url": req.creation_base_url,
    });
    if let (Some(checkpoint), Some(node)) = (checkpoint, payload["node"].as_object()) {
        if verified_atomic_checkpoint(checkpoint, &w.active_stage)? {
            let ordinal = node["id"]
                .as_str()
                .unwrap()
                .rsplit('-')
                .next()
                .unwrap()
                .parse::<usize>()
                .map_err(|_| root_axes_invalid())?;
            payload["node_mode"] = serde_json::json!({"kind":"root_axis_atomic",
                "axis_id":checkpoint.root_axis_ids.get(ordinal).ok_or_else(root_axes_invalid)?,"node_id":node["id"]});
        }
    }
    if w.active_stage == "writing" && stage.catalog_binding.is_none() {
        payload["plan"]["chapters"] = serde_json::json!(w
            .chapters
            .iter()
            .map(|chapter| serde_json::json!({
                "id": chapter.id, "title": chapter.title, "goal": chapter.goal,
            }))
            .collect::<Vec<_>>());
    }
    if matches!(w.active_stage.as_str(), "direction" | "detail") && stage.catalog_binding.is_none()
    {
        let binding = stage.root_axes_binding.as_ref();
        // Protected old work can continue saved nodes without silently adopting
        // a new registry. It never recompiles those roots into new attributes.
        // Atomic version15 nodes require their complete frozen proof even when
        // answers/body protect the snapshot. Config drift fails safely; it must
        // never turn max_next_goals=0 into an unbound leaf authorization.
        let protected_legacy_node = !root_axes_refresh_allowed(stored)
            && stage.preparation_contract_version < PREPARATION_CONTRACT_VERSION
            && (node.is_some() || stage.review_checkpoint.is_some());
        if !protected_legacy_node {
            let binding = binding.ok_or_else(root_axes_invalid)?;
            if root_axes::load().as_ref().ok() != Some(binding) {
                return Err(root_axes_invalid());
            }
            payload["root_axes_binding"] = serde_json::to_value(binding).unwrap();
        }
    }
    if let Some(binding) = &stage.catalog_binding {
        payload["trusted_catalog"] = serde_json::to_value(binding).unwrap();
        if let Some(topic) = topic {
            let catalog_topic = binding
                .stage(&w.active_stage)
                .and_then(|s| s.topics.iter().find(|t| t.id == topic.id))
                .ok_or_else(catalog_invalid)?;
            payload["root_limit"] = serde_json::json!(catalog_topic.root_slot_ids.len());
        }
    }
    if let Some(review) = &stage.review_checkpoint {
        if review.input_fingerprint != input_fingerprint(req, stored)
            || review.pool_fingerprint != pool_fingerprint(stage)
            || review.preparation_contract_version != PREPARATION_CONTRACT_VERSION
            || review.next_index >= stage.questions.len()
            || !(8..=96).contains(&review.unit_budget)
        {
            return Err(invalid("脑暴输入已变化，请恢复当前进度后重试"));
        }
        let mut questions = vec![];
        for original in review_chunk(stage, review) {
            let mut question = serde_json::to_value(original).unwrap();
            question.as_object_mut().unwrap().remove("context_details");
            for option in question["options"].as_array_mut().unwrap() {
                option.as_object_mut().unwrap().remove("details");
            }
            questions.push(serde_json::json!({"question":question,"topic":review_question_topic(stage, original).ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?,
                "ancestor_conditions":question_ancestor_conditions(original, stage)?}));
        }
        payload["kind"] = serde_json::json!("review");
        payload["topic"] = serde_json::Value::Null;
        payload["node"] = serde_json::Value::Null;
        payload["review_questions"] = serde_json::json!(questions);
    }
    Ok(payload)
}

fn compiled_plan_flags(stored: &BrainstormStoredState) -> Vec<String> {
    // Match Python str.splitlines while preserving every original nonblank line.
    stored
        .brief_edits
        .get("open_flags")
        .map(|value| {
            value
                .split(|c| {
                    matches!(
                        c,
                        '\n' | '\r'
                            | '\u{000b}'
                            | '\u{000c}'
                            | '\u{001c}'
                            | '\u{001d}'
                            | '\u{001e}'
                            | '\u{0085}'
                            | '\u{2028}'
                            | '\u{2029}'
                    )
                })
                .filter(|line| {
                    !line
                        .trim_matches(|c: char| c.is_whitespace() || c == '\u{001f}')
                        .is_empty()
                })
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}

fn validate_compiled_free_plan(
    stored: &BrainstormStoredState,
    value: &serde_json::Value,
) -> Result<(), Failure> {
    let workflow = stored.stage_workflow.as_ref().unwrap();
    let stage = workflow.current();
    let object = value
        .as_object()
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if object.keys().any(|key| {
        !matches!(
            key.as_str(),
            "topics" | "chapters" | "open_flags" | "memory_brief"
        )
    }) || value["chapters"] != serde_json::json!([])
        || value["open_flags"] != serde_json::json!(compiled_plan_flags(stored))
        || value.get("memory_brief").is_some_and(|memory| memory != "")
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let topics = value["topics"]
        .as_array()
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let limits = BuildLimits::configured();
    stage
        .root_axes_binding
        .as_ref()
        .ok_or_else(root_axes_invalid)?
        .validate_topics(
            &workflow.active_stage,
            topics,
            stage_limits()["max_topics"].as_u64().unwrap() as usize,
            root_limit_for_stage(stage, &workflow.active_stage, &limits),
        )
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))
}

fn apply_plan(stored: &mut BrainstormStoredState, value: serde_json::Value) -> Result<(), Failure> {
    let plan: Plan = serde_json::from_value(value)
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let w = stored.stage_workflow.as_mut().unwrap();
    if let Some(binding) = &w.current().catalog_binding {
        let stage = binding.stage(&w.active_stage).ok_or_else(catalog_invalid)?;
        let expected_topics: Vec<_> = stage
            .topics
            .iter()
            .map(|topic| Topic {
                id: topic.id.clone(),
                title: topic.title.clone(),
                goal: topic.goal.clone(),
                chapter_id: (w.active_stage == "writing").then(|| topic.id.clone()),
                root_axis_ids: vec![],
            })
            .collect();
        if serde_json::to_value(&plan.topics).unwrap() != serde_json::json!(expected_topics)
            || !plan.memory_brief.is_empty()
            || !plan.open_flags.is_empty()
            || (w.active_stage == "writing"
                && (plan.chapters.len() != stage.topics.len()
                    || plan
                        .chapters
                        .iter()
                        .zip(&stage.topics)
                        .any(|(chapter, topic)| {
                            chapter.id != topic.id
                                || chapter.title != topic.title
                                || chapter.goal != topic.goal
                        })))
        {
            return Err(catalog_invalid());
        }
    }
    let mut topic_ids = HashSet::new();
    let mut chapter_ids = HashSet::new();
    let max = stage_limits()["max_topics"].as_u64().unwrap() as usize;
    if plan.topics.is_empty()
        || plan.topics.len() > max
        || plan.topics.iter().any(|t| {
            t.id.trim().is_empty()
                || t.title.trim().is_empty()
                || t.goal.trim().is_empty()
                || !topic_ids.insert(&t.id)
        })
        || plan.chapters.len() > max
        || plan.chapters.iter().any(|c| {
            c.id.trim().is_empty()
                || c.title.trim().is_empty()
                || c.goal.trim().is_empty()
                || !chapter_ids.insert(&c.id)
        })
        || (w.active_stage == "writing"
            && (plan.chapters.is_empty()
                || plan.topics.iter().any(|t| {
                    !t.chapter_id
                        .as_ref()
                        .is_some_and(|id| chapter_ids.contains(id))
                })
                || plan.chapters.iter().any(|c| {
                    !plan
                        .topics
                        .iter()
                        .any(|t| t.chapter_id.as_ref() == Some(&c.id))
                })))
        || (w.active_stage != "writing"
            && (!plan.chapters.is_empty() || plan.topics.iter().any(|t| t.chapter_id.is_some())))
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    if w.active_stage == "writing" {
        w.chapters = plan
            .chapters
            .into_iter()
            .map(|c| Chapter {
                id: c.id,
                title: c.title,
                goal: c.goal,
                status: "pending".into(),
                question_ids: vec![],
                instruction_id: None,
                operation_id: None,
            })
            .collect();
    }
    let stage = w.current_mut();
    stage.progress.total_batches = plan.topics.len() + 1;
    stage.topics = plan.topics;
    if !plan.memory_brief.trim().is_empty() {
        stored.memory_brief = plan.memory_brief;
    }
    stored.open_flags = plan.open_flags;
    Ok(())
}

fn normalized_questions(
    value: serde_json::Value,
    generation: &str,
    batch: usize,
) -> Result<Vec<BrainstormQuestion>, Failure> {
    normalized_questions_for_limits(value, generation, batch, &BuildLimits::configured())
}

fn normalized_questions_for_limits(
    value: serde_json::Value,
    generation: &str,
    batch: usize,
    limits: &BuildLimits,
) -> Result<Vec<BrainstormQuestion>, Failure> {
    normalized_questions_for_contract(value, generation, batch, limits, false, false)
}

fn normalized_questions_for_contract(
    value: serde_json::Value,
    generation: &str,
    batch: usize,
    limits: &BuildLimits,
    catalog_tree: bool,
    atomic_roots: bool,
) -> Result<Vec<BrainstormQuestion>, Failure> {
    let mut questions: Vec<BrainstormQuestion> = serde_json::from_value(value["questions"].clone())
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let max = limits.max_questions_per_topic;
    let mut ids = BTreeMap::new();
    for (index, q) in questions.iter().enumerate() {
        if !is_enumerated_brainstorm_question(q)
            || ids
                .insert(
                    q.id.clone(),
                    format!("stage-{}-{}-{}", generation, batch, index),
                )
                .is_some()
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
    }
    if questions.is_empty()
        || questions.len() > max
        || !questions.iter().any(|q| q.parent_question_id.is_none())
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let max_depth = limits.max_depth;
    for q in &questions {
        let mut current = q;
        let mut depth = 1;
        let mut path = HashSet::new();
        while let Some(parent_id) = &current.parent_question_id {
            if !path.insert(&current.id) {
                return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
            }
            let Some(parent) = questions.iter().find(|p| &p.id == parent_id) else {
                return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
            };
            if !parent
                .options
                .iter()
                .any(|o| current.parent_option_id.as_ref() == Some(&o.id))
            {
                return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
            }
            depth += 1;
            current = parent;
        }
        if depth > max_depth
            || (atomic_roots && (depth != 1 || q.parent_option_id.is_some()))
            || (q.parent_question_id.is_none() && q.parent_option_id.is_some())
            || (depth < max_depth
                && !catalog_tree
                && !atomic_roots
                && q.options.iter().any(|option| {
                    !questions.iter().any(|c| {
                        c.parent_question_id.as_ref() == Some(&q.id)
                            && c.parent_option_id.as_ref() == Some(&option.id)
                    })
                }))
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
    }
    for q in &mut questions {
        q.id = ids[&q.id].clone();
        q.parent_question_id = q.parent_question_id.as_ref().map(|id| ids[id].clone());
        // Staged traversal owns ordering; legacy four-layer hints must not run.
        q.exploration_stage = None;
    }
    Ok(questions)
}

fn valid_goal(goal: &str) -> bool {
    !goal.trim().is_empty()
        && goal.chars().count() <= 80
        && !brainstorm_question_fingerprint(goal).is_empty()
}

fn apply_pool_review(
    stored: &mut BrainstormStoredState,
    req: &BrainstormTurnRequest,
    value: serde_json::Value,
) -> Result<String, Failure> {
    let result: ReviewResult = serde_json::from_value(value)
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let stage = stored.stage_workflow.as_ref().unwrap().current();
    let checkpoint = stage
        .review_checkpoint
        .as_ref()
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if checkpoint.preparation_contract_version != PREPARATION_CONTRACT_VERSION
        || checkpoint.input_fingerprint != input_fingerprint(req, stored)
        || checkpoint.pool_fingerprint != pool_fingerprint(stage)
        || checkpoint.next_index >= stage.questions.len()
        || !(8..=96).contains(&checkpoint.unit_budget)
        || !review_is_allowed(stored)
    {
        return Err(invalid("脑暴输入已变化，请恢复当前进度后重试"));
    }
    let accepted_codes = [
        "topic_scope",
        "confirmed_decision_reopened",
        "confirmed_decision_conflict",
        "parent_condition_conflict",
        "exclusion_conflict",
        "cleared_constraint_conflict",
        "candidate_as_confirmed",
        "source_permission_revoked",
    ];
    let mut codes = HashSet::new();
    let mut references = HashSet::new();
    let decisions = brainstorm_decision_context(stored);
    let valid_ids: HashSet<_> = decisions
        .iter()
        .enumerate()
        .filter(|(_, decision)| {
            let source = decision["answer_source"].as_str().unwrap_or("user");
            source == "user_excluded"
                || (source == "user"
                    && (decision["answer"]
                        .as_str()
                        .is_some_and(|text| !text.trim().is_empty())
                        || decision["selected_options"]
                            .as_array()
                            .is_some_and(|items| !items.is_empty())
                        || decision["user_inputs"]
                            .as_array()
                            .is_some_and(|items| !items.is_empty())))
        })
        .map(|(index, _)| format!("decision_{}", index + 1))
        .collect();
    if result.codes.len() > 8
        || result.decision_ids.len() > 8
        || result
            .codes
            .iter()
            .any(|code| !accepted_codes.contains(&code.as_str()) || !codes.insert(code))
        || result
            .decision_ids
            .iter()
            .any(|id| !valid_ids.contains(id) || !references.insert(id))
        || (result.review == "pass"
            && (!result.codes.is_empty() || !result.decision_ids.is_empty()))
        || (result.review == "reject"
            && (result.codes.is_empty()
                || (result.codes.iter().any(|code| {
                    matches!(
                        code.as_str(),
                        "confirmed_decision_reopened"
                            | "confirmed_decision_conflict"
                            | "exclusion_conflict"
                    )
                }) && result.decision_ids.is_empty())))
        || !matches!(result.review.as_str(), "pass" | "reject")
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    if result.review == "reject" {
        archive_preparation_pool(
            stored,
            "semantic_review_rejected",
            result.codes,
            result.decision_ids,
        )?;
        return Ok("preparing".into());
    }
    // Recompute the same frozen request slice, never skip a fixed number of
    // questions after a smaller, option-weighted review succeeds.
    let next = checkpoint.next_index + review_chunk(stage, checkpoint).len();
    let complete = next == stage.questions.len();
    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
    if complete {
        stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
        stage.review_checkpoint = None;
    } else {
        stage.review_checkpoint.as_mut().unwrap().next_index = next;
    }
    if complete && stage.progress.completed_batches >= stage.progress.total_batches {
        Ok(select_next(stored))
    } else {
        Ok("preparing".into())
    }
}

fn apply_preparation_step(
    stored: &mut BrainstormStoredState,
    req: &BrainstormTurnRequest,
    value: serde_json::Value,
) -> Result<String, Failure> {
    let stage = stored.stage_workflow.as_ref().unwrap().current();
    let catalog_stage = stored.stage_workflow.as_ref().unwrap().active_stage.clone();
    if catalog_stage == "detail" && !stage.input_phase.is_empty() && stage.input_phase != "complete" {
        return apply_input_preflight(stored, value);
    }
    if stage.content_mode {
        return apply_content_questions(stored, value);
    }
    if stage.catalog_binding.is_some() && value.get("questions").is_some() {
        return Err(catalog_invalid());
    }
    if stage.review_checkpoint.is_some() {
        return apply_pool_review(stored, req, value);
    }
    // Current free requests use compiled plan/topic_plan/node exclusively.
    // A stale complete-tree response must not bypass the manifest/root proof.
    // Direct apply_batch remains the local legacy adapter; protected v12/v13
    // stages, catalog and writing retain their existing entry contracts.
    if stage.preparation_contract_version == PREPARATION_CONTRACT_VERSION
        && stage.catalog_binding.is_none()
        && matches!(catalog_stage.as_str(), "direction" | "detail")
        && value.get("questions").is_some()
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    if stage.progress.completed_batches == 0
        || (stage.topic_checkpoint.is_none() && value.get("questions").is_some())
    {
        // Older test/local adapters may return a complete topic. New requests
        // use topic_plan/node, while retaining the existing atomic tree gate.
        if stage.progress.completed_batches == 0
            && stage.catalog_binding.is_none()
            && stage.preparation_contract_version == PREPARATION_CONTRACT_VERSION
            && matches!(catalog_stage.as_str(), "direction" | "detail")
        {
            validate_compiled_free_plan(stored, &value)?;
        }
        let phase = apply_batch(stored, value)?;
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint = None;
        return Ok(phase);
    }
    let fingerprint = input_fingerprint(req, stored);
    if stage.topic_checkpoint.is_none() {
        let result: TopicPlanResult = serde_json::from_value(value)
            .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
        let limits = BuildLimits::configured();
        let mut goals = HashSet::new();
        if result.root_goals.is_empty()
            || (stage.catalog_binding.is_none()
                && result.root_goals.len() > root_limit_for_stage(stage, &catalog_stage, &limits))
            || result.root_goals.iter().any(|goal| {
                !valid_goal(goal)
                    || !goals.insert(if stage.catalog_binding.is_some() {
                        goal.clone()
                    } else {
                        brainstorm_question_fingerprint(goal)
                    })
            })
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        if let Some(binding) = &stage.catalog_binding {
            let topic = current_topic(stored)?;
            let configured = binding
                .stage(&catalog_stage)
                .and_then(|stage| stage.topics.iter().find(|t| t.id == topic.id))
                .ok_or_else(catalog_invalid)?;
            if result.root_goals
                != configured
                    .root_slot_ids
                    .iter()
                    .map(|id| brainstorm_catalog::slot_goal(id))
                    .collect::<Vec<_>>()
            {
                return Err(catalog_invalid());
            }
        }
        let root_axis_ids = if stage.catalog_binding.is_none()
            && matches!(catalog_stage.as_str(), "direction" | "detail")
        {
            stage
                .root_axes_binding
                .as_ref()
                .ok_or_else(root_axes_invalid)?
                .validate_goals(&catalog_stage, &result.root_goals)
                .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?
        } else {
            vec![]
        };
        let topic = current_topic(stored)?.clone();
        if stage.catalog_binding.is_none()
            && matches!(catalog_stage.as_str(), "direction" | "detail")
            && (topic.root_axis_ids.is_empty() || root_axis_ids != topic.root_axis_ids)
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        let atomic_root_questions = stage.catalog_binding.is_none()
            && matches!(catalog_stage.as_str(), "direction" | "detail");
        if atomic_root_questions {
            stage
                .root_axes_binding
                .as_ref()
                .ok_or_else(root_axes_invalid)?
                .validate_atomic_goals(&catalog_stage, &result.root_goals)
                .map_err(|_| root_axes_invalid())?;
        }
        let build_id = uuid::Uuid::new_v4().to_string();
        let batch = stage.progress.completed_batches;
        let frontier = result
            .root_goals
            .iter()
            .enumerate()
            .map(|(index, goal)| NodeGoal {
                id: format!("stage-{}-{}-{}", build_id, batch, index),
                goal: goal.trim().into(),
                depth: 1,
                parent_question_id: None,
                parent_option_id: None,
            })
            .collect();
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.topic_checkpoint = Some(TopicCheckpoint {
            preparation_contract_version: PREPARATION_CONTRACT_VERSION,
            topic_id: topic.id.clone(),
            topic,
            build_id,
            input_fingerprint: fingerprint,
            questions: vec![],
            frontier,
            next_node_index: result.root_goals.len(),
            memory_brief: String::new(),
            limits,
            catalog_binding: stage.catalog_binding.clone(),
            root_axes_binding: stage.root_axes_binding.clone(),
            root_axis_ids,
            atomic_root_questions,
            root_value_selections: vec![],
            catalog_stage: stage
                .catalog_binding
                .as_ref()
                .map(|_| catalog_stage.clone()),
        });
        stage.progress.updated_at_ms = now();
        return Ok("preparing".into());
    }
    // Every validation runs on a private clone. Failed nodes cannot change the
    // saved frontier, earlier candidates, references, chapters or memory.
    let mut checkpoint = stage.topic_checkpoint.as_ref().unwrap().clone();
    if (checkpoint.preparation_contract_version != PREPARATION_CONTRACT_VERSION
        && !legacy_root_checkpoint_allowed(stored, checkpoint.preparation_contract_version))
        || checkpoint.input_fingerprint != fingerprint
        || checkpoint.topic_id != current_topic(stored)?.id
    {
        return Err(invalid("脑暴输入已变化，请恢复当前进度后重试"));
    }
    let node = checkpoint
        .frontier
        .first()
        .cloned()
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if checkpoint.preparation_contract_version == PREPARATION_CONTRACT_VERSION
        && matches!(catalog_stage.as_str(), "direction" | "detail")
        && stage.catalog_binding.is_none()
        && root_axes_refresh_allowed(stored)
    {
        if checkpoint.root_axis_ids != checkpoint.topic.root_axis_ids
            || serde_json::to_value(&checkpoint.topic).unwrap()
                != serde_json::to_value(current_topic(stored)?).unwrap()
        {
            return Err(root_axes_invalid());
        }
        let binding = checkpoint
            .root_axes_binding
            .as_ref()
            .ok_or_else(root_axes_invalid)?;
        if stage.root_axes_binding.as_ref() != Some(binding) || checkpoint.root_axis_ids.is_empty()
        {
            return Err(root_axes_invalid());
        }
        let eligible = binding
            .eligible(&catalog_stage)
            .map_err(|_| root_axes_invalid())?;
        let roots = checkpoint
            .root_axis_ids
            .iter()
            .map(|id| {
                eligible
                    .iter()
                    .find(|axis| axis["id"].as_str() == Some(id))
                    .and_then(|axis| axis["goal"].as_str())
                    .map(str::to_owned)
                    .ok_or_else(root_axes_invalid)
            })
            .collect::<Result<Vec<_>, _>>()?;
        if binding
            .validate_goals(&catalog_stage, &roots)
            .map_err(|_| root_axes_invalid())?
            != checkpoint.root_axis_ids
        {
            return Err(root_axes_invalid());
        }
        if node.depth == 1 {
            let index = node
                .id
                .rsplit('-')
                .next()
                .and_then(|index| index.parse::<usize>().ok())
                .ok_or_else(root_axes_invalid)?;
            if roots.get(index) != Some(&node.goal) {
                return Err(root_axes_invalid());
            }
        }
    }
    let atomic_roots = verified_atomic_checkpoint(&checkpoint, &catalog_stage)?;
    let (max_options, max_next_goals) = node_budget(&checkpoint, &node, &catalog_stage)?;
    let mut result: NodeResult = serde_json::from_value(value)
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if atomic_roots && !result.next_goals.is_empty() {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    if !atomic_roots && result.root_value_selection.is_some() {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let question = &mut result.question;
    question.id = node.id.clone();
    question.parent_question_id = node.parent_question_id.clone();
    question.parent_option_id = node.parent_option_id.clone();
    question.exploration_stage = None;
    if atomic_roots {
        let selection = result
            .root_value_selection
            .as_ref()
            .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
        let ordinal = checkpoint.questions.len();
        validate_rendered_root_question(
            checkpoint
                .root_axes_binding
                .as_ref()
                .ok_or_else(root_axes_invalid)?,
            &catalog_stage,
            &checkpoint.root_axis_ids[ordinal],
            &checkpoint.topic,
            selection,
            question,
        )?;
        checkpoint.root_value_selections.push(RootValueCheckpoint {
            node_id: node.id.clone(),
            selection: selection.clone(),
        });
    }
    let saved_questions: Vec<_> = stage
        .questions
        .iter()
        .chain(&checkpoint.questions)
        .collect();
    let condition_prefix = prepared_question_prefix(question, &saved_questions)
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if let Some(binding) = &checkpoint.catalog_binding {
        let slot = binding
            .slot(&catalog_stage, &node.goal)
            .ok_or_else(catalog_invalid)?;
        let topic = current_topic(stored)?;
        let expected_next: Vec<_> = slot.methods.iter().enumerate().filter(|(_, method)| !method.child_slot_ids.is_empty()).map(|(index, method)| serde_json::json!({"option_id":((b'a' + index as u8) as char).to_string(),"goals":method.child_slot_ids.iter().map(|id| brainstorm_catalog::slot_goal(id)).collect::<Vec<_>>()})).collect();
        if question.prompt != format!("{condition_prefix}{}", slot.prompt_body)
            || question.dimension_id != slot.topic_id
            || question.dimension != topic.title
            || question.why_now != slot.why_now
            || question.question_type != "single_choice"
            || !question.required
            || !question.allow_custom
            || question.answer_template != "补充你的具体实施方法。"
            || question.single_choice_reason != "各方法服务同一对象，请选择一种实施方式"
            || question.options.len() != slot.methods.len()
            || question.options.iter().zip(&slot.methods).enumerate().any(
                |(index, (option, method))| {
                    option.id != ((b'a' + index as u8) as char).to_string()
                        || option.label != method.label
                        || option.description != method.description
                        || option.details != method.tradeoff
                        || option.recommended != (index == 0)
                },
            )
            || serde_json::to_value(&result.next_goals).unwrap() != serde_json::json!(expected_next)
            || !result.memory_brief.is_empty()
            || !question.context_details.is_empty()
        {
            return Err(catalog_invalid());
        }
    }
    let mut labels = HashSet::new();
    let valid_text = |text: &str, maximum| {
        !text.trim().is_empty()
            && text.chars().count() <= maximum
            && candidate_copy_is_complete(text)
    };
    if !is_enumerated_brainstorm_question(question)
        || question.options.len() > max_options
        || (question.question_type == "single_choice"
            && question.single_choice_reason.trim().is_empty())
        || !valid_text(&question.prompt, 60)
        || !question.prompt.starts_with(&condition_prefix)
        || !valid_text(&question.why_now, 80)
        || question.options.iter().any(|option| {
            option.id.len() != 1
                || !(b'a'..b'a' + question.options.len() as u8).contains(&option.id.as_bytes()[0])
                || !valid_text(&option.label, 24)
                || !valid_text(&option.description, 80)
                || !labels.insert(candidate_label_key(&option.label))
        })
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let fingerprint = brainstorm_question_fingerprint(&question.prompt);
    let body_fingerprint =
        brainstorm_question_fingerprint(prepared_question_body(question, &saved_questions));
    if body_fingerprint.is_empty()
        || saved_questions.iter().any(|saved| {
            brainstorm_question_fingerprint(&saved.prompt) == fingerprint
                || brainstorm_question_fingerprint(prepared_question_body(saved, &saved_questions))
                    == body_fingerprint
        })
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let mut seen_options = HashSet::new();
    let mut seen_goals = HashSet::new();
    let mut next_count = 0;
    for next in &result.next_goals {
        if !seen_options.insert(&next.option_id)
            || !question
                .options
                .iter()
                .any(|option| option.id == next.option_id)
            || (checkpoint.catalog_binding.is_none()
                && !atomic_roots
                && node.depth < checkpoint.limits.max_depth
                && next.goals.is_empty())
            || next.goals.iter().any(|goal| {
                !valid_goal(goal)
                    || !seen_goals.insert((
                        next.option_id.clone(),
                        if checkpoint.catalog_binding.is_some() {
                            goal.clone()
                        } else {
                            brainstorm_question_fingerprint(goal)
                        },
                    ))
            })
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        next_count += next.goals.len();
    }
    if next_count > max_next_goals
        || (checkpoint.catalog_binding.is_none()
            && !atomic_roots
            && node.depth < checkpoint.limits.max_depth
            && seen_options.len() != question.options.len())
        || (node.depth == checkpoint.limits.max_depth && next_count != 0)
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    checkpoint.frontier.remove(0);
    for next in result.next_goals {
        for goal in next.goals {
            checkpoint.frontier.push(NodeGoal {
                id: format!(
                    "stage-{}-{}-{}",
                    checkpoint.build_id,
                    stage.progress.completed_batches,
                    checkpoint.next_node_index
                ),
                goal: goal.trim().into(),
                depth: node.depth + 1,
                parent_question_id: Some(question.id.clone()),
                parent_option_id: Some(next.option_id.clone()),
            });
            checkpoint.next_node_index += 1;
        }
    }
    checkpoint.questions.push(result.question);
    let minimum = checkpoint
        .frontier
        .iter()
        .try_fold(checkpoint.questions.len(), |total, goal| {
            total.checked_add(if let Some(binding) = &checkpoint.catalog_binding {
                binding.subtree_size(&catalog_stage, &goal.goal)?
            } else {
                if atomic_roots {
                    1
                } else {
                    checkpoint.limits.minimum_nodes(goal.depth)
                }
            })
        })
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if minimum > checkpoint.limits.max_questions_per_topic {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let memory = result.memory_brief.trim();
    if !memory.is_empty()
        && !checkpoint
            .memory_brief
            .split("\n\n")
            .any(|part| part == memory)
    {
        if !checkpoint.memory_brief.is_empty() {
            checkpoint.memory_brief.push_str("\n\n");
        }
        checkpoint.memory_brief.push_str(memory);
    }
    if checkpoint.frontier.is_empty() {
        let phase = apply_batch_with_stable_ids(
            stored,
            serde_json::json!({
                "questions": checkpoint.questions, "memory_brief": checkpoint.memory_brief,
            }),
        )?;
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint = None;
        return Ok(phase);
    }
    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
    stage.topic_checkpoint = Some(checkpoint);
    stage.progress.updated_at_ms = now();
    Ok("preparing".into())
}

fn apply_content_questions(
    stored: &mut BrainstormStoredState,
    value: serde_json::Value,
) -> Result<String, Failure> {
    let workflow = stored.stage_workflow.as_ref().unwrap();
    let stage = workflow.current();
    if stage.preparation_contract_version != CONTENT_PREPARATION_CONTRACT_VERSION
        || stage.catalog_binding.is_some()
        || stage.root_axes_binding.is_some()
        || stage.progress.completed_batches != 0
        || stage.progress.total_batches > 1
        || value.as_object().is_none_or(|object| {
            !object.contains_key("questions")
                || !object.contains_key("content_proofs")
                || object.keys().any(|key| !matches!(key.as_str(),
                    "questions" | "content_proofs" | "no_new_decision_reason"))
        })
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let raw_questions: Vec<BrainstormQuestion> = serde_json::from_value(value["questions"].clone())
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let raw_proofs: Vec<ContentQuestionProof> =
        serde_json::from_value(value["content_proofs"].clone())
            .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let no_new_decision_reason = value.get("no_new_decision_reason")
        .and_then(serde_json::Value::as_str)
        .unwrap_or_default()
        .to_owned();
    if raw_questions.len() > 4
        || raw_proofs.len() != raw_questions.len()
        || (raw_questions.is_empty()
            && (no_new_decision_reason.trim().is_empty()
                || no_new_decision_reason.chars().count() > 180
                || !candidate_copy_is_complete(&no_new_decision_reason)))
        || (!raw_questions.is_empty() && !no_new_decision_reason.trim().is_empty())
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let effective_root = stored
        .brief_edits
        .get("root_request")
        .unwrap_or(&stored.root_request);
    let decisions = brainstorm_decision_context(stored);
    let mut proof_by_id = BTreeMap::new();
    for proof in raw_proofs {
        let quote = proof.anchor_quote.trim();
        let effect = proof.document_effect.trim();
        let anchored = match proof.anchor_source.as_str() {
            "root_request" => effective_root.contains(quote),
            "user_decision" => decisions.iter().any(|decision| {
                decision["answer_source"] == "user"
                    && ((decision["manually_edited"] != true
                        && decision["answer"].as_str().is_some_and(|answer| answer.contains(quote)))
                        || decision["user_inputs"].as_array().is_some_and(|inputs| {
                            inputs.iter().any(|input| input.as_str().is_some_and(|text| text.contains(quote)))
                        }))
            }),
            "manual_edit" => stored.brief_edits.values().any(|text| text.contains(quote)),
            _ => false,
        };
        let mut effect_keys = HashSet::new();
        if proof.question_id.trim().is_empty()
            || quote.chars().count() < 2
            || quote.chars().count() > 24
            || effect.is_empty()
            || effect.chars().count() > 160
            || !anchored
            || !(2..=5).contains(&proof.option_effects.len())
            || proof.option_effects.iter().any(|item| {
                item.trim().is_empty()
                    || item.chars().count() > 160
                    || !effect_keys.insert(brainstorm_question_fingerprint(item))
            })
            || proof_by_id.insert(proof.question_id.clone(), proof).is_some()
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
    }
    let mut question_keys = stage
        .questions
        .iter()
        .map(|question| brainstorm_question_fingerprint(&question.prompt))
        .collect::<HashSet<_>>();
    // A new stage must not present a previously confirmed question as a new
    // decision. Keep this gate at the persistence boundary for old sidecars.
    question_keys.extend(stored.turns.iter()
        .filter(|turn| turn.answer.source == "user")
        .map(|turn| brainstorm_question_fingerprint(&turn.question.prompt)));
    let valid_text = |text: &str, maximum: usize| {
        !text.trim().is_empty()
            && text.chars().count() <= maximum
            && candidate_copy_is_complete(text)
    };
    let mut ordered_proofs = Vec::with_capacity(raw_questions.len());
    for question in &raw_questions {
        let proof = proof_by_id.remove(&question.id)
            .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
        let mut labels = HashSet::new();
        let prompt_key = brainstorm_question_fingerprint(&question.prompt);
        if !question.required
            || !question.allow_custom
            || question.parent_question_id.is_some()
            || question.parent_option_id.is_some()
            || question.input_clarification.is_some()
            || !valid_text(&question.prompt, 60)
            || !valid_text(&question.why_now, 80)
            || !valid_text(&question.dimension, 60)
            || question.dimension_id.trim().is_empty()
            || question.dimension_id.chars().count() > 80
            || (question.question_type == "single_choice"
                && !valid_text(&question.single_choice_reason, 80))
            || !question_keys.insert(prompt_key)
            || !question.prompt.contains(proof.anchor_quote.trim())
            || proof.option_effects.len() != question.options.len()
            || question.options.iter().any(|option| {
                !valid_text(&option.label, 24)
                    || !valid_text(&option.description, 80)
                    || !labels.insert(candidate_label_key(&option.label))
            })
        {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        ordered_proofs.push(proof);
    }
    if !proof_by_id.is_empty() {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let limits = BuildLimits::configured();
    let questions = if raw_questions.is_empty() {
        Vec::new()
    } else {
        normalized_questions_for_contract(
            value,
            &stage.generation_id,
            1,
            &limits,
            false,
            true,
        )?
    };
    for (proof, question) in ordered_proofs.iter_mut().zip(&questions) {
        proof.question_id = question.id.clone();
    }
    let no_new_decision_reason = raw_questions.is_empty()
        .then_some(no_new_decision_reason);
    let audit_sha256 = content_audit_hash(&ordered_proofs, &no_new_decision_reason);
    let workflow = stored.stage_workflow.as_mut().unwrap();
    if workflow.active_stage == "writing" {
        if !workflow.chapters.is_empty() || workflow.active_chapter_id.is_some() {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        workflow.chapters.push(Chapter {
            id: "body".into(),
            title: "创作正文".into(),
            goal: "按当前需求、完整有效确认简报和所选技能规定的结构，创作本次任务的完整正文；保留原有分节、必需内容、固定方法与证据位置。".into(),
            status: "pending".into(),
            question_ids: questions.iter().map(|question| question.id.clone()).collect(),
            instruction_id: None,
            operation_id: None,
        });
        workflow.active_chapter_id = Some("body".into());
    }
    let stage = workflow.current_mut();
    stage.questions.extend(questions);
    stage.content_proofs = ordered_proofs;
    stage.content_no_new_decision_reason = no_new_decision_reason;
    stage.content_audit_sha256 = Some(audit_sha256);
    stage.progress.completed_batches = 1;
    stage.progress.total_batches = 1;
    stage.progress.percent = 100;
    stage.progress.updated_at_ms = now();
    stage.progress.estimated_remaining_seconds = Some(0);
    Ok(select_next(stored))
}

fn apply_input_preflight(
    stored: &mut BrainstormStoredState,
    value: serde_json::Value,
) -> Result<String, Failure> {
    let preflight = value.get("input_preflight")
        .filter(|item| item["schema_version"] == 1)
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?
        .clone();
    let needs = preflight["needs"].as_array()
        .filter(|items| items.len() <= 6)
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if preflight["as_of_local_date"].as_str().is_none()
        || preflight["timezone"].as_str().is_none()
        || needs.iter().any(|need| {
            need["id"].as_str().is_none()
                || need["receipts"].as_array().is_none_or(|items| items.len() > 4)
        })
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let status = value["input_plan_status"].as_str()
        .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    let questions: Vec<BrainstormQuestion> = serde_json::from_value(value["questions"].clone())
        .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
    if questions.len() > 7 || questions.iter().any(|question| {
        !question.required || !question.allow_custom
            || question.parent_question_id.is_some() || question.parent_option_id.is_some()
            || question.input_clarification.as_ref().and_then(|marker| marker["kind"].as_str())
                .is_none_or(|kind| !matches!(kind, "report_period" | "source_confirmation"))
            || question.options.iter().any(|option| option.recommended)
    }) {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    if (status == "question") != !questions.is_empty()
        || (questions.is_empty()
            && !matches!(status, "ready" | "ready_with_gaps_or_attestation" | "not_applicable"))
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let reset_unanswered_content = {
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        stage.content_mode
            && stage.content_audit_sha256.is_some()
            && stage.input_preflight.as_ref() != Some(&preflight)
            && !content_pool_has_user_work(stored, stage)
    };
    if reset_unanswered_content {
        stored.current_question = None;
    }
    let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
    if reset_unanswered_content {
        archive_content_preparation(stage, "input_preflight_changed");
        stage.questions.retain(|question| question.input_clarification.is_some());
        stage.progress.completed_batches = 0;
        stage.progress.total_batches = 0;
        stage.progress.percent = 0;
        stage.progress.updated_at_ms = now();
        stage.progress.estimated_remaining_seconds = None;
        stage.generation_id = uuid::Uuid::new_v4().to_string();
    }
    // A retry can replace only unanswered clarification candidates. Confirmed
    // turns and prior receipts remain in the same session for audit and resume.
    stage.questions.retain(|question| question.input_clarification.is_none()
        || stored.turns.iter().any(|turn| turn.question.id == question.id));
    for question in questions {
        if stage.questions.iter().any(|saved| saved.id == question.id) {
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        stage.questions.push(question);
    }
    stage.input_preflight = Some(preflight);
    if status == "question" {
        stage.input_phase = "questions".into();
        Ok(select_next(stored))
    } else {
        stage.input_phase = "complete".into();
        if stage.progress.total_batches > 0
            && stage.progress.completed_batches == stage.progress.total_batches {
            Ok(select_next(stored))
        } else {
            Ok("preparing".into())
        }
    }
}

fn apply_batch_with_stable_ids(
    stored: &mut BrainstormStoredState,
    value: serde_json::Value,
) -> Result<String, Failure> {
    apply_batch_internal(stored, value, true)
}

fn apply_batch(
    stored: &mut BrainstormStoredState,
    value: serde_json::Value,
) -> Result<String, Failure> {
    apply_batch_internal(stored, value, false)
}

fn apply_batch_internal(
    stored: &mut BrainstormStoredState,
    value: serde_json::Value,
    preserve_ids: bool,
) -> Result<String, Failure> {
    let w = stored.stage_workflow.as_ref().unwrap();
    let stage = w.current();
    if stage.progress.completed_batches == 0 {
        apply_plan(stored, value)?;
    } else {
        let batch = stage.progress.completed_batches;
        let memory = value["memory_brief"]
            .as_str()
            .unwrap_or_default()
            .trim()
            .to_string();
        let checked = if preserve_ids {
            let limits = stage
                .topic_checkpoint
                .as_ref()
                .map(|checkpoint| checkpoint.limits.clone())
                .unwrap_or_else(BuildLimits::configured);
            normalized_questions_for_contract(
                value.clone(),
                &stage.generation_id,
                batch,
                &limits,
                stage.catalog_binding.is_some(),
                stage
                    .topic_checkpoint
                    .as_ref()
                    .map(|checkpoint| verified_atomic_checkpoint(checkpoint, &w.active_stage))
                    .transpose()?
                    .unwrap_or(false),
            )?
        } else {
            normalized_questions(value.clone(), &stage.generation_id, batch)?
        };
        let questions = if preserve_ids {
            // Validate with the original whole-tree gate, retaining the IDs
            // already committed in this topic's checkpoints across retries.
            serde_json::from_value::<Vec<BrainstormQuestion>>(value["questions"].clone())
                .map_err(|_| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?
        } else {
            checked
        };
        let mut question_fingerprints = stage
            .questions
            .iter()
            .map(|question| brainstorm_question_fingerprint(&question.prompt))
            .collect::<HashSet<_>>();
        if questions.iter().any(|question| {
            stage.questions.iter().any(|saved| saved.id == question.id)
                || !question_fingerprints.insert(brainstorm_question_fingerprint(&question.prompt))
        }) {
            // Compare against every saved topic before changing memory or graph
            // bindings. The bounded prompt summary is only model context.
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
        if !memory.is_empty() && !stored.memory_brief.split("\n\n").any(|part| part == memory) {
            if !stored.memory_brief.is_empty() {
                stored.memory_brief.push_str("\n\n");
            }
            stored.memory_brief.push_str(&memory);
        }
        let chapter_id = stage.topics[batch - 1].chapter_id.clone();
        let w = stored.stage_workflow.as_mut().unwrap();
        if let Some(chapter_id) = chapter_id {
            let chapter = w
                .chapters
                .iter_mut()
                .find(|c| c.id == chapter_id)
                .ok_or_else(|| brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"))?;
            chapter
                .question_ids
                .extend(questions.iter().map(|q| q.id.clone()));
        }
        w.current_mut().questions.extend(questions);
    }
    let w = stored.stage_workflow.as_mut().unwrap();
    let stage = w.current_mut();
    let p = &mut stage.progress;
    p.completed_batches += 1;
    p.updated_at_ms = now();
    p.percent = p.completed_batches * 100 / p.total_batches.max(1);
    p.estimated_remaining_seconds = Some(
        ((p.updated_at_ms - p.started_at_ms).max(0) as u64 / p.completed_batches as u64)
            * (p.total_batches - p.completed_batches) as u64
            / 1000,
    );
    if p.completed_batches < p.total_batches {
        return Ok("preparing".into());
    }
    if w.active_stage == "writing" {
        w.active_chapter_id = w.chapters.first().map(|c| c.id.clone());
    }
    Ok(select_next(stored))
}

async fn run_batches(state: &Arc<AppState>, req: &BrainstormTurnRequest, generation: &str) {
    loop {
        let snapshot = with_brainstorm_session_guard(state, req.session_id.trim(), false, |conn| {
            Ok(repo::get(conn, req.session_id.trim())?)
        });
        let Ok(Some(mut session)) = snapshot else {
            return;
        };
        let Ok(mut stored) = decode(&session) else {
            return;
        };
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        if stage.status != "thinking" || stage.generation_id != generation {
            return;
        }
        if let Some(recovery) = &stage.automatic_recovery {
            let remaining = recovery.retry_at_ms.saturating_sub(now());
            if remaining > 0 {
                // Re-read durable state after every short wait, so cancellation,
                // changed authority and exhausted budgets cannot dispatch stale work.
                tokio::time::sleep(Duration::from_millis((remaining as u64).min(1_000))).await;
                continue;
            }
        }
        let prepared = refresh_catalog_binding(state, &mut stored)
            .and_then(|_| refresh_root_axes_binding(&mut stored))
            .map(|_| refresh_checkpoint_input(&mut stored, req))
            .and_then(|_| prepare_pool_review(&mut stored, req))
            .and_then(|_| reserve_recovery_dispatch(&mut stored, req));
        let mut dispatched = false;
        let result = match prepared {
            Ok(reserved) => {
                if reserved {
                    // A dispatch is charged before leaving the process, including
                    // interruptions. Revision/generation still guard final publish.
                    if persist(
                        state,
                        req.session_id.trim(),
                        session.revision,
                        "preparing",
                        &stored,
                    )
                    .is_err()
                    {
                        return;
                    }
                    session.revision += 1;
                }
                dispatched = true;
                request_batch(state, req, &stored).await.and_then(|value| {
                    // Validation is transactional even if a later gate rejects.
                    // Rejected candidates cannot alter memory or chapter bindings.
                    let mut accepted = stored.clone();
                    let phase = apply_preparation_step(&mut accepted, req, value)?;
                    if let Some(recovery) = &mut accepted
                        .stage_workflow
                        .as_mut()
                        .unwrap()
                        .current_mut()
                        .automatic_recovery
                    {
                        recovery.request_in_flight = false;
                        recovery.last_failure_code = None;
                        recovery.retry_at_ms = 0;
                    }
                    stored = accepted;
                    Ok(phase)
                })
            }
            Err(error) => Err(error),
        };
        let phase = match result {
            Ok(phase) => phase,
            Err((status, Json(error))) => {
                record_preparation_failure(&mut stored, status, error, dispatched);
                "preparing".into()
            }
        };
        // Revision compares the entire input snapshot, while generation identity
        // prevents a cancelled/retried worker from publishing into a later stage.
        if persist(
            state,
            req.session_id.trim(),
            session.revision,
            &phase,
            &stored,
        )
        .is_err()
        {
            return;
        }
        if phase != "preparing"
            || stored.stage_workflow.as_ref().unwrap().current().status == "failed"
        {
            return;
        }
    }
}

fn verify_chapter_completion(
    state: &AppState,
    session: &str,
    chapter: &Chapter,
) -> Result<(), Failure> {
    let verified = with_brainstorm_session_guard(state, session, false, |conn| {
        let Some(id) = chapter.operation_id.as_deref() else {
            return Ok(false);
        };
        let Some(operation) = creation_operation::get(conn, session, id)? else {
            return Ok(false);
        };
        let Some(result) = operation.result else {
            return Ok(false);
        };
        let Some(history) = creation_history::get_by_id(conn, operation.history_id)? else {
            return Ok(false);
        };
        let trace: Vec<serde_json::Value> = history
            .agent_trace_json
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok())
            .unwrap_or_default();
        let reviewed = trace.iter().any(|e| {
            e["type"] == "delivery.checked"
                && e["run_id"] == result["run_id"]
                && e["data"]["review"]["status"] == "pass"
        });
        Ok(operation.status == "completed"
            && result["type"] == "run.completed"
            && result["run_id"].as_str().is_some_and(|id| !id.is_empty())
            && reviewed
            && result["data"]["delivery_review"]["status"] == "pass"
            && !history.generated_content.trim().is_empty()
            && result["data"]["document"] == history.generated_content
            && result["data"]["revision_no"] == history.revision_no)
    })?;
    if !verified {
        return Err(brainstorm_error(
            StatusCode::CONFLICT,
            "BRAINSTORM_CHAPTER_UNVERIFIED",
            "本章还未完成通用 Agent Loop 交付验收，请继续或重试本章创作",
        ));
    }
    Ok(())
}

fn reject_unbound_chapter(req: &AgentRunRequest) -> Result<(), (StatusCode, String)> {
    if req.brainstorm_chapter_id.is_some() {
        Err((
            StatusCode::CONFLICT,
            "BRAINSTORM_CHAPTER_NOT_CONFIRMED".into(),
        ))
    } else {
        Ok(())
    }
}

/// A confirmed chapter owns one durable operation. Its resumable attempt is the
/// current checkpoint; a separate whole-document task is outside that scope.
/// After the whole article completes, retain ordinary pending edits and hide
/// only the precisely identified legacy seed of this v2 session's root request.
/// No operation or event is deleted or rewritten by this context projection.
pub(super) fn scope_pending_operations(
    state: &AppState,
    req: &AgentRunRequest,
    pending: Vec<serde_json::Value>,
) -> Result<Vec<serde_json::Value>, (StatusCode, String)> {
    let Some(session_id) = req.session_id.as_deref() else {
        return Ok(pending);
    };
    let session = with_brainstorm_session_guard(state, session_id, false, |conn| {
        Ok(repo::get(conn, session_id)?)
    })
    .map_err(|(status, _)| (status, "脑暴创作上下文无法恢复".into()))?;
    let Some(session) = session else {
        return Ok(pending);
    };
    let value: serde_json::Value = serde_json::from_str(&session.state_json).unwrap_or_default();
    if value["stage_workflow"]["version"] != 2 {
        return Ok(pending);
    }
    if req.brainstorm_chapter_id.is_some() {
        return Ok(Vec::new());
    }
    let legacy_prefix = format!("legacy-operation-{}-", session_id);
    let current_root = value["root_request"].as_str().unwrap_or_default().trim();
    Ok(pending
        .into_iter()
        .filter(|item| {
            let seeded = item["operation_id"]
                .as_str()
                .is_some_and(|id| id.starts_with(&legacy_prefix));
            let instruction = item["instruction"].as_str().unwrap_or_default().trim();
            !(seeded
                && !instruction.is_empty()
                && (instruction == session.root_request.trim() || instruction == current_root))
        })
        .collect())
}

fn chapter_skill_binding_unavailable() -> (StatusCode, String) {
    (
        StatusCode::CONFLICT,
        "BRAINSTORM_SKILL_BINDING_UNAVAILABLE".into(),
    )
}

fn chapter_skill_id(value: &serde_json::Value) -> Option<String> {
    let id = value
        .get("id")
        .or_else(|| value.get("clientSkillKey"))
        .or_else(|| value.get("client_skill_key"))?;
    id.as_str()
        .map(str::trim)
        .filter(|id| !id.is_empty())
        .map(str::to_string)
        .or_else(|| id.as_i64().map(|id| id.to_string()))
}

fn chapter_skill_role(value: &serde_json::Value) -> Result<&str, (StatusCode, String)> {
    match value
        .get("workflowRole")
        .or_else(|| value.get("workflow_role"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or("primary")
    {
        "primary" => Ok("primary"),
        "support" => Ok("support"),
        _ => Err(chapter_skill_binding_unavailable()),
    }
}

fn chapter_skill_source<'a>(
    value: &'a serde_json::Value,
    role: &str,
    original_request: &str,
) -> Result<&'a str, (StatusCode, String)> {
    if role == "support" {
        return match value
            .get("selectionSource")
            .or_else(|| value.get("selection_source"))
            .and_then(serde_json::Value::as_str)
        {
            None | Some("dependency") => Ok("dependency"),
            _ => Err(chapter_skill_binding_unavailable()),
        };
    }
    let title = value
        .get("title")
        .and_then(serde_json::Value::as_str)
        .map(str::trim)
        .filter(|title| !title.is_empty())
        .ok_or_else(chapter_skill_binding_unavailable)?;
    let literal_mention = original_request.contains(&format!("@{title}"));
    match value
        .get("selectionSource")
        .or_else(|| value.get("selection_source"))
        .and_then(serde_json::Value::as_str)
    {
        Some("user") if literal_mention => Ok("user"),
        Some("user") => Err(chapter_skill_binding_unavailable()),
        Some("automatic") => Ok("automatic"),
        None => Ok(if literal_mention { "user" } else { "automatic" }),
        _ => Err(chapter_skill_binding_unavailable()),
    }
}

fn bind_chapter_skills(
    stored: &BrainstormStoredState,
    req: &mut AgentRunRequest,
) -> Result<(), (StatusCode, String)> {
    if stored.selected_skills.is_empty() {
        let content_mode = stored.stage_workflow.as_ref().is_some_and(|workflow| {
            workflow.stages.len() == 3 && workflow.stages.iter().all(|stage|
                stage.preparation_contract_version == CONTENT_PREPARATION_CONTRACT_VERSION)
        });
        if content_mode {
            // The desktop sends its discoverable Skill catalog alongside every
            // request. A v17 no-Skill chapter must carry the saved empty
            // selection; catalog candidates are not user selections.
            req.selected_skills.clear();
            req.available_skills = Some(Vec::new());
            req.explicit_skill_ids.clear();
            req.brainstorm_skill_binding = None;
        }
        return Ok(());
    }
    let available = req
        .available_skills
        .as_ref()
        .unwrap_or(&req.selected_skills);
    let mut bound_profiles = Vec::with_capacity(stored.selected_skills.len());
    let mut bound_skills = Vec::with_capacity(stored.selected_skills.len());
    let mut frozen = Vec::with_capacity(stored.selected_skills.len());
    let mut explicit_ids = Vec::new();
    let mut seen = HashSet::new();
    let mut primary_count = 0;
    for selection in &stored.selected_skills {
        let id = chapter_skill_id(selection).ok_or_else(chapter_skill_binding_unavailable)?;
        if !seen.insert(id.clone()) {
            return Err(chapter_skill_binding_unavailable());
        }
        let role = chapter_skill_role(selection)?;
        let source = chapter_skill_source(selection, role, &stored.root_request)?;
        if role == "primary" {
            primary_count += 1;
        }
        let matches = available
            .iter()
            .filter(|candidate| chapter_skill_id(candidate).as_deref() == Some(id.as_str()))
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            return Err(chapter_skill_binding_unavailable());
        }
        let candidate = matches[0];
        let selected_fields = selection
            .as_object()
            .ok_or_else(chapter_skill_binding_unavailable)?;
        let candidate_fields = candidate
            .as_object()
            .ok_or_else(chapter_skill_binding_unavailable)?;
        for (key, expected) in selected_fields {
            if matches!(
                key.as_str(),
                "workflowRole"
                    | "workflow_role"
                    | "selectionSource"
                    | "selection_source"
                    | "exampleDocumentAvailable"
            ) {
                continue;
            }
            if candidate_fields.get(key) != Some(expected) {
                return Err(chapter_skill_binding_unavailable());
            }
        }
        let mut executable = candidate.clone();
        let object = executable
            .as_object_mut()
            .ok_or_else(chapter_skill_binding_unavailable)?;
        object.insert("workflowRole".into(), serde_json::json!(role));
        object.insert("selectionSource".into(), serde_json::json!(source));
        let mut executable_for_fingerprint = executable.clone();
        executable_for_fingerprint
            .as_object_mut()
            .unwrap()
            .remove("exampleDocumentAvailable");
        bound_profiles.push(executable);
        bound_skills.push(serde_json::json!({"id": id, "role": role, "selection_source": source}));
        frozen.push(
            serde_json::json!({"id": id, "role": role, "selection_source": source,
            "profile": selection, "executable_profile": executable_for_fingerprint}),
        );
        if source == "user" {
            explicit_ids.push(id);
        }
    }
    if primary_count == 0 {
        return Err(chapter_skill_binding_unavailable());
    }
    let fingerprint = sha256_hex(&canonical_json(
        &serde_json::json!({"version": 1, "skills": frozen}),
    ));
    req.brainstorm_skill_binding = Some(serde_json::json!({
        "version": 1, "fingerprint": fingerprint, "skills": bound_skills,
    }));
    req.selected_skills = bound_profiles.clone();
    req.available_skills = Some(bound_profiles);
    req.explicit_skill_ids = explicit_ids;
    Ok(())
}

fn confirmed_user_report_period(stored: &BrainstormStoredState) -> Option<serde_json::Value> {
    let detail = stored.stage_workflow.as_ref()?.stages.iter().find(|stage| stage.id == "detail")?;
    let saved = detail.confirmed_period.as_ref()?;
    if saved["source"] != "user" {
        return None;
    }
    let timezone = saved["timezone"].as_str()?;
    stored.turns.iter().filter(|turn| turn.answer.source == "user"
        && detail.questions.iter().any(|question| question.id == turn.question.id)
        && turn.question.input_clarification.as_ref()
            .is_some_and(|marker| marker["kind"] == "report_period"))
        .find_map(|turn| {
            let resolved = confirmed_answer_period(&turn.question, &turn.answer, timezone).ok()?;
            (resolved["start"] == saved["start"]
                && resolved["end"] == saved["end"]
                && resolved["timezone"] == saved["timezone"])
                .then(|| resolved)
        })
}

fn content_source_gap_contract(stored: &BrainstormStoredState) -> Option<serde_json::Value> {
    let workflow = stored.stage_workflow.as_ref()?;
    if !stored.selected_skills.is_empty()
        || workflow.stages.len() != 3
        || workflow.stages.iter().any(|stage| stage.preparation_contract_version != CONTENT_PREPARATION_CONTRACT_VERSION)
    {
        return None;
    }
    let detail = workflow.stages.iter().find(|stage| stage.id == "detail")?;
    let preflight = detail.input_preflight.as_ref()?;
    let period = confirmed_user_report_period(stored)?;
    let needs = preflight["needs"].as_array()?;
    if needs.is_empty() {
        return None;
    }
    let sanitized = needs.iter().map(|need| {
        Some(serde_json::json!({
            "id": need["id"].as_str()?,
            "label": need["label"].as_str()?,
            "status": need["status"].as_str()?,
        }))
    }).collect::<Option<Vec<_>>>()?;
    Some(serde_json::json!({
        "schema_version": "brainstorm.content-source-gap.v1",
        "preparation_contract_version": CONTENT_PREPARATION_CONTRACT_VERSION,
        "confirmed_period": period,
        "input_preflight": {
            "period": preflight["period"],
            "timezone": preflight["timezone"],
            "needs": sanitized,
        },
    }))
}

pub(super) fn bind_chapter_request(
    state: &AppState,
    req: &mut AgentRunRequest,
) -> Result<(), (StatusCode, String)> {
    let Some(session_id) = req.session_id.clone() else {
        return reject_unbound_chapter(req);
    };
    let session_id = session_id.as_str();
    let session = with_brainstorm_session_guard(state, session_id, false, |conn| {
        Ok(repo::get(conn, session_id)?)
    })
    .map_err(|(code, _)| (code, "当前脑暴会话不可用".into()))?;
    let Some(session) = session else {
        return reject_unbound_chapter(req);
    };
    // Legacy sessions may contain older question shapes. Chapter enforcement
    // must not parse or rewrite those records on an ordinary Agent request.
    if !serde_json::from_str::<serde_json::Value>(&session.state_json)
        .ok()
        .is_some_and(|value| value["stage_workflow"]["version"] == 2)
    {
        return reject_unbound_chapter(req);
    }
    let stored: BrainstormStoredState = serde_json::from_str(&session.state_json)
        .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "脑暴简报无法恢复".into()))?;
    let Some(w) = stored.stage_workflow.as_ref() else {
        return Ok(());
    };
    if session.phase == "completed" && req.brainstorm_chapter_id.is_none() {
        let mut brief = serde_json::to_value(brainstorm_response(
            session_id,
            &session.phase,
            session.revision,
            &stored,
        ))
        .unwrap();
        if let Some(period) = confirmed_user_report_period(&stored) {
            brief["confirmed_report_period"] = period;
        }
        // Candidate clicks and source-level period metadata do not prove that
        // this report's metric rows and values have been verified. Keep the
        // private gap contract on later edits until a row-level receipt exists.
        if let Some(gap_contract) = content_source_gap_contract(&stored) {
            brief["content_source_gap"] = gap_contract;
        }
        for field in [
            "stage_workflow",
            "archived_questions",
            "continuation_directions",
            "invalidated_question_ids",
        ] {
            brief.as_object_mut().unwrap().remove(field);
        }
        req.creation_brief = Some(brief);
        req.creation_mode = "brainstorm".into();
        return Ok(());
    }
    let chapter = w
        .chapters
        .iter()
        .find(|c| {
            Some(&c.id) == w.active_chapter_id.as_ref()
                && Some(&c.id) == req.brainstorm_chapter_id.as_ref()
        })
        .filter(|c| c.status == "writing")
        .ok_or((
            StatusCode::CONFLICT,
            "BRAINSTORM_CHAPTER_NOT_CONFIRMED".into(),
        ))?;
    if req.instruction_id.as_ref() != chapter.instruction_id.as_ref()
        || req
            .resume_operation_id
            .as_ref()
            .is_some_and(|id| Some(id) != chapter.operation_id.as_ref())
    {
        return Err((
            StatusCode::CONFLICT,
            "BRAINSTORM_CHAPTER_OPERATION_MISMATCH".into(),
        ));
    }
    bind_chapter_skills(&stored, req)?;
    req.creation_mode = "brainstorm".into();
    req.root_request = Some(stored.root_request.clone());
    let mut brief = serde_json::to_value(brainstorm_response(
        session_id,
        &session.phase,
        session.revision,
        &stored,
    ))
    .unwrap();
    brief["chapter_context"] = serde_json::json!({"id": chapter.id, "title": chapter.title, "goal": chapter.goal,
        "index": w.chapters.iter().position(|c| c.id == chapter.id).unwrap() + 1, "total": w.chapters.len(),
        "confirmed_decisions": brief["decisions"].as_array().unwrap().iter().filter(|d| d["question_id"].as_str().is_some_and(|id| chapter.question_ids.iter().any(|q| q == id))).cloned().collect::<Vec<_>>(),
        "outline": w.chapters.iter().map(|c| serde_json::json!({"id":c.id,"title":c.title,"goal":c.goal})).collect::<Vec<_>>()});
    if let Some(period) = confirmed_user_report_period(&stored) {
        brief["confirmed_report_period"] = period;
    }
    if let Some(gap_contract) = content_source_gap_contract(&stored) {
        brief["content_source_gap"] = gap_contract;
    }
    for field in [
        "stage_workflow",
        "archived_questions",
        "continuation_directions",
        "invalidated_question_ids",
    ] {
        brief.as_object_mut().unwrap().remove(field);
    }
    req.creation_brief = Some(brief);
    let (original_instruction, has_document) =
        with_brainstorm_session_guard(state, session_id, false, |conn| {
            let original = chapter
                .operation_id
                .as_deref()
                .map(|id| creation_operation::get(conn, session_id, id))
                .transpose()?
                .flatten();
            let context = creation_history::get_session_context(conn, session_id)?;
            Ok((
                original.map(|operation| operation.instruction),
                context.is_some_and(|context| !context.latest.generated_content.trim().is_empty()),
            ))
        })
        .map_err(|(code, _)| (code, "章节创作记录无法恢复".into()))?;
    // One chapter is one deliverable. Preservation, chapter order and Agent Loop
    // execution belong to the trusted chapter contract, not extra task verbs in
    // the router's instruction. Keep an existing operation's instruction exact
    // so recovery neither overwrites history nor changes its idempotent identity.
    req.generation.user_prompt = original_instruction.unwrap_or_else(|| {
        format!(
            "{}第 {} 章《{}》，本章目标：{}。",
            if has_document {
                "在当前文档末尾续写"
            } else {
                "撰写"
            },
            w.chapters.iter().position(|c| c.id == chapter.id).unwrap() + 1,
            chapter.title,
            chapter.goal
        )
    });
    Ok(())
}

fn has_protected_chapters(w: &StageWorkflow) -> bool {
    w.chapters
        .iter()
        .any(|c| matches!(c.status.as_str(), "writing" | "completed"))
}

fn belongs_to_editable_chapter(w: &StageWorkflow, question_id: &str) -> bool {
    w.active_stage == "writing"
        && matches!(w.current().status.as_str(), "answering" | "ready")
        && !w.chapters.iter().any(|c| c.status == "writing")
        && w.chapters.iter().any(|c| {
            Some(&c.id) == w.active_chapter_id.as_ref()
                && matches!(c.status.as_str(), "answering" | "ready")
                && c.instruction_id.is_none()
                && c.operation_id.is_none()
                && c.question_ids.iter().any(|id| id == question_id)
        })
}

fn revise_saved_answer(
    stored: &mut BrainstormStoredState,
    req: &BrainstormTurnRequest,
    revision: i64,
) -> Result<String, Failure> {
    let w = stored.stage_workflow.as_ref().unwrap();
    let id = req
        .question_id
        .as_deref()
        .ok_or_else(|| invalid("请选择要修改的已确认问题"))?;
    if has_protected_chapters(w) && !belongs_to_editable_chapter(w, id) {
        return Err(brainstorm_error(
            StatusCode::CONFLICT,
            "BRAINSTORM_CHAPTER_REVIEW_REQUIRED",
            "已有章节正在创作或已完成，请先回看正文；为保护已完成内容，不能直接改写上游确认",
        ));
    }
    let question = stored
        .turns
        .iter()
        .find(|t| t.question.id == id)
        .map(|t| t.question.clone())
        .ok_or_else(|| invalid("该问题尚未确认或已失效"))?;
    let input_kind = question.input_clarification.as_ref()
        .and_then(|marker| marker["kind"].as_str()).map(str::to_owned);
    let stage_index = w
        .stages
        .iter()
        .position(|s| s.questions.iter().any(|q| q.id == id))
        .ok_or_else(|| invalid("该问题已不属于当前阶段"))?;
    let answer = if req.action == "clear_saved_answer" {
        Some(BrainstormAnswer {
            selected_option_ids: vec![],
            custom_text: String::new(),
            source: "user_cleared".into(),
        })
    } else if req.action == "revise_answer" {
        let answer = req
            .answer
            .clone()
            .ok_or_else(|| invalid("请选择有效答案"))?;
        if answer.source != "user" || !validate_brainstorm_answer(&question, &answer) {
            return Err(invalid("请选择有效答案"));
        }
        Some(answer)
    } else {
        None
    };
    let revised_period = if input_kind.as_deref() == Some("report_period") {
        answer.as_ref().filter(|answer| answer.source == "user")
            .map(|answer| {
                let timezone = w.stages[stage_index].input_preflight.as_ref()
                    .and_then(|value| value["timezone"].as_str())
                    .unwrap_or("Asia/Shanghai");
                confirmed_answer_period(&question, answer, timezone)
            }).transpose()?
    } else {
        None
    };
    let content_input_revision = w.stages[stage_index].content_mode
        && matches!(input_kind.as_deref(), Some("report_period" | "source_confirmation"));
    let mut invalidated: HashSet<String> = [id.to_string()].into_iter().collect();
    if input_kind.as_deref() == Some("report_period") {
        invalidated.extend(w.stages[stage_index].questions.iter().filter(|candidate|
            candidate.input_clarification.as_ref().and_then(|marker| marker["kind"].as_str())
                == Some("source_confirmation")).map(|candidate| candidate.id.clone()));
    }
    if content_input_revision {
        invalidated.extend(w.stages[stage_index].questions.iter()
            .filter(|candidate| candidate.input_clarification.is_none())
            .map(|candidate| candidate.id.clone()));
    }
    loop {
        let before = invalidated.len();
        for q in &w.stages[stage_index].questions {
            if q.parent_question_id
                .as_ref()
                .is_some_and(|p| invalidated.contains(p))
            {
                invalidated.insert(q.id.clone());
            }
        }
        if before == invalidated.len() {
            break;
        }
    }
    for stage in w.stages.iter().skip(stage_index + 1) {
        invalidated.extend(stage.questions.iter().map(|q| q.id.clone()));
    }
    let mut archive = Vec::new();
    stored.turns.retain(|turn| {
        if invalidated.contains(&turn.question.id) {
            archive.push(BrainstormArchivedQuestion {
                question: turn.question.clone(),
                answer: Some(turn.answer.clone()),
            });
            false
        } else {
            true
        }
    });
    // Preserve all invalidated content candidates, including unanswered ones.
    if content_input_revision {
        for q in w.stages[stage_index].questions.iter()
            .filter(|candidate| candidate.input_clarification.is_none())
        {
            if !archive.iter().any(|saved| saved.question.id == q.id) {
                archive.push(BrainstormArchivedQuestion {
                    question: q.clone(),
                    answer: None,
                });
            }
        }
    }
    // Preserve the complete invalidated downstream trees, including unchosen branches.
    for stage in w.stages.iter().skip(stage_index + 1) {
        for q in &stage.questions {
            if !archive.iter().any(|a| a.question.id == q.id) {
                archive.push(BrainstormArchivedQuestion {
                    question: q.clone(),
                    answer: None,
                });
            }
        }
    }
    stored.archived_questions.extend(archive);
    stored.invalidated_question_ids = invalidated.iter().cloned().collect();
    let previous_brief_edits = stored.brief_edits.clone();
    let previous_input_revisions = stored.user_input_revisions.clone();
    for id in &invalidated {
        stored.brief_edits.remove(id);
        stored.user_input_revisions.remove(id);
    }
    let reset_current_content = content_input_revision
        && stored.stage_workflow.as_ref().unwrap().stages[stage_index]
            .content_audit_sha256.is_some();
    let w = stored.stage_workflow.as_mut().unwrap();
    for stage in w.stages.iter_mut().skip(stage_index) {
        let fields = stage.questions.iter().filter(|question|
            invalidated.contains(&question.id)).map(|question| question.id.clone())
            .collect::<HashSet<_>>();
        archive_content_brief_edits(stage, &previous_brief_edits,
            &previous_input_revisions, &fields,
            if content_input_revision {"confirmed_input_revised"} else {"answer_revised"});
    }
    if reset_current_content {
        let stage = &mut w.stages[stage_index];
        archive_content_preparation(stage, "confirmed_input_revised");
        stage.questions.retain(|question| question.input_clarification.is_some());
        stage.progress.completed_batches = 0;
        stage.progress.total_batches = 0;
        stage.progress.percent = 0;
        stage.progress.updated_at_ms = now();
        stage.progress.estimated_remaining_seconds = None;
    }
    for stage in w.stages.iter_mut().skip(stage_index + 1) {
        if stage.content_mode {
            archive_content_preparation(stage, "upstream_answer_revised");
        }
        stage.questions.clear();
        stage.topics.clear();
        archive_topic_checkpoint(stage, "input_changed");
        stage.review_checkpoint = None;
        stage.preparation_progress = None;
        stage.status = "pending".into();
        stage.error = None;
        stage.generation_id = uuid::Uuid::new_v4().to_string();
        stage.progress = Progress {
            completed_batches: 0,
            total_batches: 0,
            percent: 0,
            started_at_ms: now(),
            updated_at_ms: now(),
            estimated_remaining_seconds: None,
        };
    }
    w.active_stage = w.stages[stage_index].id.clone();
    if w.active_stage != "writing" {
        w.chapters.clear();
        w.active_chapter_id = None;
    }
    if let Some(answer) = answer {
        stored.user_input_revisions.insert(id.to_string(), revision);
        stored.turns.push(BrainstormStoredTurn { question, answer });
        if input_kind.as_deref() == Some("report_period") {
            let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
            stage.confirmed_period = revised_period;
            stage.input_phase = "pending".into();
            stage.status = "thinking".into();
            stage.generation_id = uuid::Uuid::new_v4().to_string();
            stored.current_question = None;
            Ok("preparing".into())
        } else if content_input_revision {
            let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
            stage.input_phase = "pending".into();
            stage.status = "thinking".into();
            stage.generation_id = uuid::Uuid::new_v4().to_string();
            stored.current_question = None;
            Ok("preparing".into())
        } else {
            Ok(select_next(stored))
        }
    } else {
        w.current_mut().status = "answering".into();
        if let Some(chapter) = w
            .chapters
            .iter_mut()
            .find(|c| Some(&c.id) == w.active_chapter_id.as_ref())
        {
            chapter.status = "answering".into();
        }
        stored.current_question = Some(question);
        if input_kind.as_deref() == Some("report_period") {
            let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
            stage.confirmed_period = None;
            stage.input_phase = "questions".into();
        } else if content_input_revision {
            stored.stage_workflow.as_mut().unwrap().current_mut().input_phase = "questions".into();
        }
        Ok("exploring".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::StorageManager;
    use serde_json::{json, Value};
    use std::sync::atomic::{AtomicUsize, Ordering};

    const SESSION: &str = "stages-test-session";

    #[test]
    fn stage_batch_budget_accepts_integer_range_and_defaults_invalid_values() {
        for (configured, expected) in [
            (None, 210),
            (Some("30"), 30),
            (Some("210"), 210),
            (Some("600"), 600),
            (Some(" 60 "), 60),
            (Some("+90"), 210),
            (Some(""), 210),
            (Some("invalid"), 210),
            (Some("29"), 210),
            (Some("601"), 210),
            (Some("0"), 210),
            (Some("-30"), 210),
            (Some("210.0"), 210),
            (Some("2_10"), 210),
            (Some("18446744073709551616"), 210),
        ] {
            assert_eq!(stage_batch_timeout_seconds(configured), expected);
        }
    }

    #[test]
    fn stage_batch_transport_has_thirty_seconds_after_total_batch_budget() {
        for configured in [None, Some("30"), Some("210"), Some("600"), Some("601")] {
            let budget = stage_batch_timeout_seconds(configured);
            assert_eq!(stage_batch_http_timeout(configured).as_secs(), budget + 30);
        }
        assert_eq!(stage_batch_http_timeout(None), Duration::from_secs(240));
        assert_eq!(
            stage_batch_http_timeout(Some("30")),
            Duration::from_secs(60)
        );
        assert_eq!(
            stage_batch_http_timeout(Some("600")),
            Duration::from_secs(630)
        );
    }

    #[test]
    fn review_unit_budget_accepts_ascii_range_and_defaults_invalid_values() {
        for (configured, expected) in [
            (None, 8),
            (Some("8"), 8),
            (Some(" 16 "), 16),
            (Some("96"), 96),
            (Some(""), 8),
            (Some("7"), 8),
            (Some("97"), 8),
            (Some("+16"), 8),
            (Some("-8"), 8),
            (Some("8.0"), 8),
            (Some("８"), 8),
            (Some("invalid"), 8),
            (Some("18446744073709551616"), 8),
        ] {
            assert_eq!(review_unit_budget(configured), expected);
        }
    }

    fn question(id: &str, parent: Option<(&str, &str)>) -> Value {
        json!({"id":id,"dimension_id":id,"dimension":"方向","type":"single_choice","prompt":format!("确认 {id} 的设计方向？"),
            "why_now":"确定后续写作方向","required":true,"allow_custom":true,
            "parent_question_id":parent.map(|p|p.0),"parent_option_id":parent.map(|p|p.1),
            "options":[{"id":"a","label":"方案甲","description":"采取集中组织方式","recommended":true},
                       {"id":"b","label":"方案乙","description":"采取分布协作方式","recommended":false}]})
    }
    fn tree() -> Value {
        json!({"questions":[question("root",None),question("left",Some(("root","a"))),question("right",Some(("root","b")))]})
    }
    fn tree_for_topic(topic: &str) -> Value {
        let mut value = tree();
        for question in value["questions"].as_array_mut().unwrap() {
            question["prompt"] = json!(format!(
                "{}：{}",
                topic,
                question["prompt"].as_str().unwrap()
            ));
        }
        value
    }
    fn four_question_tree() -> Value {
        let mut value = tree();
        value["questions"][0]["options"]
            .as_array_mut()
            .unwrap()
            .push(json!({"id":"c","label":"方案丙","description":"采用混合组织方式","recommended":false}));
        value["questions"]
            .as_array_mut()
            .unwrap()
            .push(question("third", Some(("root", "c"))));
        value
    }
    fn plan() -> Value {
        json!({"topics":[{"id":"t","title":"方向","goal":"确认主要方向"}],"chapters":[]})
    }
    fn compiled_plan_for_request(body: &Value) -> Value {
        let stage = body["stage"].as_str().unwrap_or("direction");
        let binding = root_axes::load().unwrap();
        let limits = BuildLimits::configured();
        let topics = binding
            .compile_topics(
                stage,
                &["audience".into()],
                stage_limits()["max_topics"].as_u64().unwrap() as usize,
                limits.max_questions_per_topic / limits.minimum_nodes(1),
            )
            .unwrap();
        let raw_flags = body["brief_edits"]["open_flags"]
            .as_str()
            .unwrap_or_default();
        let flags: Vec<_> = raw_flags
            .split(|c| {
                matches!(
                    c,
                    '\n' | '\r'
                        | '\u{000b}'
                        | '\u{000c}'
                        | '\u{001c}'
                        | '\u{001d}'
                        | '\u{001e}'
                        | '\u{0085}'
                        | '\u{2028}'
                        | '\u{2029}'
                )
            })
            .filter(|line| {
                !line
                    .trim_matches(|c: char| c.is_whitespace() || c == '\u{001f}')
                    .is_empty()
            })
            .collect();
        json!({"topics":topics,"chapters":[],"open_flags":flags})
    }

    fn compiled_topic_roots(body: &Value) -> Value {
        let binding = root_axes::load().unwrap();
        let axes = binding.eligible(body["stage"].as_str().unwrap()).unwrap();
        let ids = body["topic"]["root_axis_ids"].as_array().unwrap();
        json!({"root_goals":ids.iter().map(|id| axes.iter().find(|axis| axis["id"] == *id).unwrap()["goal"].clone()).collect::<Vec<_>>()})
    }

    fn initial() -> BrainstormStoredState {
        let mut stored: BrainstormStoredState =
            serde_json::from_value(json!({"root_request":"撰写团队方案"})).unwrap();
        stored.stage_workflow = Some(StageWorkflow::new());
        // Legacy tests exercise explicit recovery. New automatic-recovery tests
        // opt into frozen budgets rather than mutating process environment.
        for stage in &mut stored.stage_workflow.as_mut().unwrap().stages {
            if stage.id != "writing" {
                stage.root_axes_binding = Some(root_axes::load().unwrap());
            }
            let mut recovery = AutomaticRecovery::configured();
            recovery.max_step_retries = 0;
            recovery.max_stage_retries = 0;
            stage.automatic_recovery = Some(recovery);
        }
        stored
    }
    fn initial_content() -> BrainstormStoredState {
        let mut stored: BrainstormStoredState = serde_json::from_value(
            json!({"root_request":"请生成GPU成本优化的周报"}),
        ).unwrap();
        stored.stage_workflow = Some(StageWorkflow::new_content());
        stored
    }
    fn content_question() -> Value {
        json!({
            "id":"cost_focus", "dimension_id":"cost_focus", "dimension":"本期成本变化",
            "type":"multi_choice", "prompt":"GPU成本优化本期应先解释哪项变化？",
            "why_now":"确定本期周报需要解释的具体成本变化。",
            "required":true, "allow_custom":true,
            "options":[
                {"id":"a","label":"核实用量变化","description":"先核对本期用量，再解释成本变化。","recommended":true},
                {"id":"b","label":"核实单价变化","description":"先核对计费单价，再解释成本变化。","recommended":false}
            ]
        })
    }

    #[test]
    fn content_writing_rejects_a_confirmed_detail_question_without_changing_state() {
        let mut stored = initial_content();
        let previous: BrainstormQuestion = serde_json::from_value(content_question()).unwrap();
        stored.turns.push(BrainstormStoredTurn { question: previous,
            answer: BrainstormAnswer { selected_option_ids: vec!["a".into()],
                source: "user".into(), ..Default::default() } });
        stored.stage_workflow.as_mut().unwrap().active_stage = "writing".into();
        let before = serde_json::to_value(&stored).unwrap();
        assert!(apply_content_questions(&mut stored, content_response()).is_err());
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
    }
    fn content_response() -> Value {
        json!({
            "questions":[content_question()],
            "content_proofs":[{
                "question_id":"cost_focus", "anchor_source":"root_request",
                "anchor_quote":"GPU成本优化", "document_effect":"确定本期周报成本分析的解释对象。",
                "option_effects":["正文解释用量变化及其证据。", "正文解释单价变化及其证据。"]
            }]
        })
    }

    fn answered_content_detail_with_inputs() -> (BrainstormStoredState, String, String, String) {
        let mut stored = initial_content();
        let period: BrainstormQuestion = serde_json::from_value(json!({
            "id":"period", "dimension_id":"report_period", "dimension":"报告周期",
            "type":"single_choice", "prompt":"这份周报覆盖哪一周？",
            "why_now":"确认报告周期。", "required":true, "allow_custom":true,
            "options":[
                {"id":"week_a","label":"上一周","description":"2026-09-21 至 2026-09-27",
                    "input_value":{"start":"2026-09-21","end":"2026-09-27","timezone":"Asia/Shanghai"}},
                {"id":"week_b","label":"本周","description":"2026-09-28 至 2026-10-04",
                    "input_value":{"start":"2026-09-28","end":"2026-10-04","timezone":"Asia/Shanghai"}}
            ], "input_clarification":{"kind":"report_period","need_ids":["period-need"]}
        })).unwrap();
        let source: BrainstormQuestion = serde_json::from_value(json!({
            "id":"source", "dimension_id":"source_confirmation", "dimension":"资料来源",
            "type":"single_choice", "prompt":"本期成本依据哪组来源？",
            "why_now":"核对来源范围。", "required":true, "allow_custom":true,
            "options":[
                {"id":"source_a","label":"账单数据","description":"使用已核对账单。"},
                {"id":"source_b","label":"待补数据","description":"在正文标明资料缺口。"}
            ], "input_clarification":{"kind":"source_confirmation","need_ids":["source-need"]}
        })).unwrap();
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "detail".into();
        let stage = workflow.current_mut();
        stage.input_phase = "complete".into();
        stage.input_preflight = Some(json!({"schema_version":1,
            "as_of_local_date":"2026-09-30","timezone":"Asia/Shanghai","needs":[]}));
        stage.confirmed_period = Some(json!({"start":"2026-09-21","end":"2026-09-27",
            "timezone":"Asia/Shanghai","source":"user"}));
        stage.questions.extend([period.clone(), source.clone()]);
        stored.turns.push(BrainstormStoredTurn {question: period.clone(),
            answer: BrainstormAnswer {selected_option_ids: vec!["week_a".into()],
                source:"user".into(), ..Default::default()}});
        stored.turns.push(BrainstormStoredTurn {question: source.clone(),
            answer: BrainstormAnswer {selected_option_ids: vec!["source_a".into()],
                source:"user".into(), ..Default::default()}});
        apply_content_questions(&mut stored, content_response()).unwrap();
        let content = stored.current_question.as_ref().unwrap().clone();
        stored.turns.push(BrainstormStoredTurn {question: content.clone(),
            answer: BrainstormAnswer {selected_option_ids: vec!["a".into()],
                source:"user".into(), ..Default::default()}});
        assert_eq!(select_next(&mut stored), "ready");
        (stored, period.id, source.id, content.id)
    }

    #[test]
    fn content_mode_routes_new_stages_and_keeps_detail_input_preflight_first() {
        let mut old = initial();
        assert!(!old.stage_workflow.as_ref().unwrap().current().content_mode);
        assert_eq!(old.stage_workflow.as_ref().unwrap().current().preparation_contract_version, 16);
        old.stage_workflow.as_mut().unwrap().active_stage = "detail".into();
        old.stage_workflow.as_mut().unwrap().current_mut().input_phase = "pending".into();
        let legacy_input = batch_payload(&request("status", 0), &old).unwrap();
        assert_eq!(legacy_input["kind"], "input_preflight");
        assert_eq!(legacy_input["content_mode"], false);
        let mut stored = initial_content();
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        assert_eq!(payload["kind"], "content_questions");
        assert_eq!(payload["stage"], "direction");
        assert!(payload.get("root_axes_binding").is_none());
        assert!(payload.get("trusted_catalog").is_none());
        assert_eq!(payload["plan"]["input_preflight"], Value::Null);
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "detail".into();
        workflow.current_mut().input_phase = "pending".into();
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        assert_eq!(payload["kind"], "input_preflight");
        assert_eq!(payload["content_mode"], true);
        let before = input_fingerprint(&request("status", 0), &stored);
        let detail = stored.stage_workflow.as_mut().unwrap().current_mut();
        detail.input_phase = "complete".into();
        detail.confirmed_period = Some(json!({"start":"2026-09-21","end":"2026-09-27","source":"user"}));
        detail.input_preflight = Some(json!({"schema_version":1,"status":"partial","needs":[]}));
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        assert_eq!(payload["kind"], "content_questions");
        assert_eq!(payload["confirmed_period"]["source"], "user");
        assert_eq!(payload["plan"]["input_preflight"]["status"], "partial");
        assert_ne!(before, input_fingerprint(&request("status", 0), &stored));
    }

    #[test]
    fn selected_skill_new_session_keeps_trusted_catalog_contract() {
        let row = brainstorm_catalog::tests::record();
        let selected = vec![brainstorm_catalog::tests::selected(&row)];
        assert!(StageWorkflow::new_for_selected_skills(&[]).current().content_mode);
        let mut stored = initial();
        stored.selected_skills = selected.clone();
        stored.stage_workflow = Some(StageWorkflow::new_for_selected_skills(&selected));
        let workflow = stored.stage_workflow.as_mut().unwrap();
        assert!(!workflow.current().content_mode);
        assert_eq!(workflow.current().preparation_contract_version, 16);
        workflow.active_stage = "detail".into();
        let (_tmp, state) = fixture(None, "http://127.0.0.1:9");
        brainstorm_catalog::tests::install(&state.storage, &row);
        assert!(refresh_catalog_binding(&state, &mut stored).unwrap());
        assert!(stored.stage_workflow.as_ref().unwrap().current().catalog_binding.is_some());
        assert_eq!(batch_payload(&request("status", 0), &stored).unwrap()["kind"], "plan");
    }

    #[test]
    fn content_questions_require_bound_proofs_and_keep_them_private() {
        let mut stored = initial_content();
        let output = content_response();
        assert_eq!(apply_preparation_step(&mut stored, &request("status", 0), output.clone()).unwrap(), "exploring");
        let workflow = stored.stage_workflow.as_ref().unwrap();
        let stage = workflow.current();
        assert_eq!(stage.questions.len(), 1);
        assert!(stage.questions[0].id.starts_with("stage-"));
        assert_eq!(stage.content_proofs[0].question_id, stage.questions[0].id);
        assert!(content_audit_valid(stage));
        assert_eq!(stage.progress.completed_batches, 1);
        let public = serde_json::to_value(workflow.for_response()).unwrap();
        let public_stage = &public["stages"][0];
        assert!(public_stage.get("content_proofs").is_none());
        assert!(public_stage.get("content_audit_sha256").is_none());
        assert!(public_stage.get("content_mode").is_none());

        for mut rejected in [
            { let mut value = output.clone(); value["content_proofs"][0]["anchor_quote"] = json!("不存在的对象"); value },
            { let mut value = output.clone(); value["questions"][0]["prompt"] = json!("本期应先解释哪项变化？"); value },
            { let mut value = output.clone(); value["content_proofs"][0]["option_effects"][1] =
                value["content_proofs"][0]["option_effects"][0].clone(); value },
            { let mut value = output.clone(); value["content_proofs"] = json!([]); value },
        ] {
            let mut fresh = initial_content();
            assert!(apply_content_questions(&mut fresh, rejected.take()).is_err());
            assert!(fresh.stage_workflow.as_ref().unwrap().current().questions.is_empty());
        }
        let mut tampered = stage.clone();
        tampered.content_proofs[0].document_effect.push_str("篡改");
        assert!(!content_audit_valid(&tampered));
    }

    #[test]
    fn content_copy_repair_is_rejected_without_changing_reviewed_candidate() {
        let mut stored = initial_content();
        apply_content_questions(&mut stored, content_response()).unwrap();
        let question = stored.current_question.as_ref().unwrap().clone();
        let mut req = request("repair_candidate_copy", 1);
        req.candidate_copy_patches = vec![label_patch(&question, 0, "直接改写已审选项")];
        let before = serde_json::to_value(&stored).unwrap();
        let failure = repair_candidate_copy(&mut stored, &req, 2).unwrap_err();
        assert_eq!(failure.1 .0["code"], "BRAINSTORM_STAGE_CONFLICT");
        assert!(failure.1 .0["message"].as_str().unwrap().contains("自定义答案"));
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        assert!(stored.stage_workflow.as_ref().unwrap().copy_repair_history.is_empty());
    }

    #[test]
    fn revising_confirmed_period_or_source_archives_answered_content_before_replanning() {
        for kind in ["period", "source"] {
            let (mut stored, period_id, source_id, content_id) =
                answered_content_detail_with_inputs();
            let old_manual = "人工修订：只比较可核验的有效算力成本";
            stored.brief_edits.insert(content_id.clone(), old_manual.into());
            stored.user_input_revisions.insert(content_id.clone(), 7);
            let mut req = request("revise_answer", 1);
            req.question_id = Some(if kind == "period" {period_id} else {source_id});
            req.answer = Some(BrainstormAnswer {
                selected_option_ids: vec![if kind == "period" {"week_b"} else {"source_b"}.into()],
                source: "user".into(), ..Default::default()
            });
            assert_eq!(revise_saved_answer(&mut stored, &req, 2).unwrap(), "preparing");
            let stage = stored.stage_workflow.as_ref().unwrap().current();
            assert_eq!(stage.input_phase, "pending");
            assert_eq!(stage.progress.completed_batches, 0);
            assert_eq!(stage.progress.total_batches, 0);
            assert!(stage.questions.iter().all(|question| question.input_clarification.is_some()));
            assert!(stage.content_proofs.is_empty());
            assert!(stage.content_audit_sha256.is_none());
            assert_eq!(stage.content_archives.len(), 1);
            let archived_edit = stage.content_brief_edit_archives.iter().find(|archive|
                archive.field_id == content_id).unwrap();
            assert_eq!(archived_edit.text, old_manual);
            assert_eq!(archived_edit.saved_revision, Some(7));
            assert!(content_audit_valid(stage));
            assert!(stored.turns.iter().all(|turn| turn.question.id != content_id));
            let saved = stored.archived_questions.iter().find(|item|
                item.question.id == content_id).unwrap();
            assert_eq!(saved.answer.as_ref().unwrap().selected_option_ids, vec!["a"]);
            assert!(!brainstorm_decision_context(&stored).iter().any(|decision|
                decision["question_id"] == content_id));
            assert!(!brainstorm_brief(&stored).contains(old_manual));
            assert!(!serde_json::to_string(&stored.stage_workflow.as_ref().unwrap().for_response())
                .unwrap().contains(old_manual));
            let restored: BrainstormStoredState = serde_json::from_value(
                serde_json::to_value(&stored).unwrap()).unwrap();
            let restored_stage = restored.stage_workflow.as_ref().unwrap().current();
            assert!(content_audit_valid(restored_stage));
            assert!(restored_stage.content_brief_edit_archives.iter().any(|archive|
                archive.field_id == content_id && archive.text == old_manual));
            assert!(stored.current_question.is_none());
            assert_eq!(batch_payload(&request("status", 2), &stored).unwrap()["kind"],
                "input_preflight");
        }
    }

    #[test]
    fn selected_content_option_method_reaches_chapter_brief_without_becoming_a_fact() {
        let (mut stored, _, _, content_id) = answered_content_detail_with_inputs();
        let content_turn = stored.turns.iter().find(|turn| turn.question.id == content_id).unwrap();
        let content_question = content_turn.question.clone();
        let description = content_turn.question.options[0].description.clone();
        let summary = brainstorm_effective_summary(&stored, content_turn);
        assert!(summary.contains("用户选择的条件做法"));
        assert!(summary.contains("不代表相关事实已经核实"));
        assert!(summary.contains(&description));
        assert_eq!(summary.matches(&description).count(), 1);
        assert!(brainstorm_brief(&stored).contains(&summary));
        let decision = brainstorm_decision_context(&stored).into_iter()
            .find(|item| item["question_id"] == content_id).unwrap();
        assert_eq!(decision["answer"], summary);

        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "writing".into();
        workflow.active_chapter_id = Some("body".into());
        workflow.chapters = vec![Chapter {id:"body".into(), title:"创作正文".into(),
            goal:"写本期周报".into(), status:"writing".into(), question_ids:vec![],
            instruction_id:Some("instruction-one".into()), operation_id:None}];
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let mut req: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"撰写正文", "session_id":SESSION,
            "brainstorm_chapter_id":"body", "instruction_id":"instruction-one"
        })).unwrap();
        bind_chapter_request(&state, &mut req).unwrap();
        let brief = req.creation_brief.unwrap();
        assert!(brief["brief_markdown"].as_str().unwrap().contains(&description));
        assert!(brief["decisions"].as_array().unwrap().iter().any(|item|
            item["question_id"] == content_id && item["summary"] == summary));

        stored.brief_edits.insert(content_id.clone(), "人工改定的本期处理方式".into());
        let edited = brainstorm_decision_context(&stored).into_iter()
            .find(|item| item["question_id"] == content_id).unwrap();
        assert_eq!(edited["answer"], "人工改定的本期处理方式");
        assert!(!brainstorm_brief(&stored).contains(&description));

        let mut legacy = initial();
        let mut legacy_question = content_question;
        legacy_question.id = "legacy-selection".into();
        legacy.stage_workflow.as_mut().unwrap().current_mut().questions.push(legacy_question.clone());
        legacy.turns.push(BrainstormStoredTurn {question:legacy_question,
            answer:BrainstormAnswer {selected_option_ids:vec!["a".into()],
                source:"user".into(), ..Default::default()}});
        assert!(!brainstorm_effective_summary(&legacy, &legacy.turns[0]).contains(&description));
    }

    #[test]
    fn content_writing_can_finish_without_filler_questions() {
        let mut stored = initial_content();
        stored.stage_workflow.as_mut().unwrap().active_stage = "writing".into();
        let output = json!({"questions":[],"content_proofs":[],
            "no_new_decision_reason":"本次内容与证据处理已由当前用户输入明确。"});
        assert_eq!(apply_content_questions(&mut stored, output).unwrap(), "ready");
        let workflow = stored.stage_workflow.as_ref().unwrap();
        assert_eq!(workflow.active_chapter_id.as_deref(), Some("body"));
        assert_eq!(workflow.chapters[0].status, "ready");
        assert!(workflow.chapters[0].question_ids.is_empty());
        assert!(content_audit_valid(workflow.current()));
        assert!(stored.current_question.is_none());
        let public = serde_json::to_value(workflow.for_response()).unwrap();
        assert_eq!(public["stages"][2]["content_no_new_decision_reason"],
            "本次内容与证据处理已由当前用户输入明确。");
        assert!(public["stages"][2].get("content_proofs").is_none());
        assert!(public["stages"][2].get("content_audit_sha256").is_none());
    }

    #[test]
    fn changed_preflight_replans_only_unanswered_content_and_archives_its_proof() {
        let mut stored = initial_content();
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "detail".into();
        workflow.current_mut().input_phase = "complete".into();
        workflow.current_mut().input_preflight = Some(json!({
            "schema_version":1,"as_of_local_date":"2026-09-29",
            "timezone":"Asia/Shanghai","needs":[]
        }));
        apply_content_questions(&mut stored, content_response()).unwrap();
        assert_eq!(stored.stage_workflow.as_ref().unwrap().current().progress.completed_batches, 1);
        let output = json!({
            "input_preflight":{"schema_version":1,"as_of_local_date":"2026-09-30",
                "timezone":"Asia/Shanghai","needs":[]},
            "input_plan_status":"ready","questions":[]
        });
        assert_eq!(apply_input_preflight(&mut stored, output).unwrap(), "preparing");
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert!(stage.questions.is_empty());
        assert_eq!(stage.progress.completed_batches, 0);
        assert!(stage.content_proofs.is_empty());
        assert_eq!(stage.content_archives.len(), 1);
        assert_eq!(stage.content_archives[0].content_proofs.len(), 1);
        assert!(content_audit_valid(stage));
        assert!(stored.current_question.is_none());
    }

    #[test]
    fn upstream_revision_archives_downstream_content_proofs_before_reset() {
        let mut stored = initial_content();
        apply_content_questions(&mut stored, content_response()).unwrap();
        let direction = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
        stored.turns.push(BrainstormStoredTurn {
            question: direction.clone(),
            answer: BrainstormAnswer {selected_option_ids: vec!["a".into()],
                source: "user".into(), ..Default::default()},
        });
        stored.stage_workflow.as_mut().unwrap().active_stage = "detail".into();
        apply_content_questions(&mut stored, content_response()).unwrap();
        let mut req = request("reopen", 1);
        req.question_id = Some(direction.id);
        assert_eq!(revise_saved_answer(&mut stored, &req, 2).unwrap(), "exploring");
        let detail = &stored.stage_workflow.as_ref().unwrap().stages[1];
        assert!(detail.questions.is_empty());
        assert!(detail.content_proofs.is_empty());
        assert!(detail.content_audit_sha256.is_none());
        assert_eq!(detail.content_archives.len(), 1);
        assert!(content_audit_valid(detail));
    }

    #[test]
    fn root_request_edit_keeps_content_mode_and_archives_prior_proofs() {
        let mut stored = initial_content();
        apply_content_questions(&mut stored, content_response()).unwrap();
        let question = stored.current_question.as_ref().unwrap().clone();
        stored.turns.push(BrainstormStoredTurn {question:question.clone(),
            answer:BrainstormAnswer {selected_option_ids:vec!["a".into()],
                source:"user".into(), ..Default::default()}});
        assert_eq!(select_next(&mut stored), "ready");
        stored.brief_edits.insert(question.id.clone(), "人工旧正文处理".into());
        stored.user_input_revisions.insert(question.id.clone(), 4);
        stored.brief_edits.insert("open_flags".into(), "人工旧待决定草稿".into());
        stored.open_flags.push("模型旧待决定".into());
        let mut req = request("edit_brief", 1);
        req.brief_edits.insert("root_request".into(), "请生成GPU利用率优化的周报".into());
        req.brief_edits.insert(question.id.clone(), "同次提交但因根需求变更失效的新稿".into());
        assert_eq!(edit_saved_brief(&mut stored, &req, 2).unwrap(), "preparing");
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert!(stage.content_mode);
        assert_eq!(stage.preparation_contract_version, 17);
        assert!(stage.questions.is_empty());
        assert_eq!(stage.content_archives.len(), 1);
        assert_eq!(stage.content_archives[0].content_proofs.len(), 1);
        assert!(stage.content_brief_edit_archives.iter().any(|archive|
            archive.field_id == question.id && archive.text == "人工旧正文处理"));
        assert!(stage.content_brief_edit_archives.iter().any(|archive|
            archive.field_id == question.id
                && archive.text == "同次提交但因根需求变更失效的新稿"
                && archive.reason == "root_request_changed_submitted"));
        assert!(stage.content_brief_edit_archives.iter().any(|archive|
            archive.field_id == "open_flags" && archive.text == "人工旧待决定草稿"));
        assert!(stage.content_root_request_archives.is_empty());
        assert!(content_audit_valid(stage));
        let brief = brainstorm_brief(&stored);
        assert!(!brief.contains("人工旧正文处理"));
        assert!(!brief.contains("同次提交但因根需求变更失效的新稿"));
        assert!(!brief.contains("人工旧待决定草稿"));
        assert!(!brief.contains("模型旧待决定"));
    }

    #[test]
    fn later_content_root_edit_archives_replaced_input_privately() {
        let mut stored = initial_content();
        stored.user_input_revisions.insert("root_request".into(), 0);
        stored.stage_workflow.as_mut().unwrap().current_mut().status = "ready".into();
        let mut first = request("edit_brief", 0);
        first.brief_edits.insert("root_request".into(),
            " 请生成GPU利用率优化的周报 ".into());
        assert_eq!(edit_saved_brief(&mut stored, &first, 1).unwrap(), "preparing");
        assert!(stored.stage_workflow.as_ref().unwrap().current()
            .content_root_request_archives.is_empty());

        stored.stage_workflow.as_mut().unwrap().current_mut().status = "ready".into();
        let mut second = request("edit_brief", 1);
        second.brief_edits.insert("root_request".into(),
            "请生成GPU机时优化的周报".into());
        assert_eq!(edit_saved_brief(&mut stored, &second, 2).unwrap(), "preparing");
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.content_root_request_archives.len(), 1);
        let archived = &stage.content_root_request_archives[0];
        assert_eq!(archived.previous_root_request, "请生成GPU利用率优化的周报");
        assert_eq!(archived.previous_root_input.as_deref(),
            Some(" 请生成GPU利用率优化的周报 "));
        assert_eq!(archived.previous_input_revision, Some(1));
        assert_eq!(archived.new_root_input, "请生成GPU机时优化的周报");
        assert_eq!(archived.new_input_revision, 2);
        assert_eq!(stored.root_request, "请生成GPU机时优化的周报");
        assert!(content_audit_valid(stage));

        let restored: BrainstormStoredState = serde_json::from_value(
            serde_json::to_value(&stored).unwrap()).unwrap();
        assert_eq!(restored.stage_workflow.as_ref().unwrap().current()
            .content_root_request_archives.len(), 1);
        assert!(restored.stage_workflow.as_ref().unwrap().for_response().current()
            .content_root_request_archives.is_empty());
        assert!(!serde_json::to_string(&restored.stage_workflow.as_ref().unwrap()
            .for_response()).unwrap().contains("请生成GPU利用率优化的周报"));

        let mut legacy = initial();
        legacy.stage_workflow.as_mut().unwrap().current_mut().status = "ready".into();
        legacy.user_input_revisions.insert("root_request".into(), 3);
        legacy.brief_edits.insert("root_request".into(), "旧 v16 输入".into());
        let mut legacy_edit = request("edit_brief", 3);
        legacy_edit.brief_edits.insert("root_request".into(), "新 v16 输入".into());
        assert_eq!(edit_saved_brief(&mut legacy, &legacy_edit, 4).unwrap(), "preparing");
        assert!(legacy.stage_workflow.as_ref().unwrap().current()
            .content_root_request_archives.is_empty());
    }

    #[test]
    fn content_brief_edit_archive_is_backward_compatible_deduplicated_and_v16_inert() {
        let stored = initial_content();
        let mut old_json = serde_json::to_value(&stored).unwrap();
        for stage in old_json["stage_workflow"]["stages"].as_array_mut().unwrap() {
            stage.as_object_mut().unwrap().remove("content_brief_edit_archives");
        }
        let restored: BrainstormStoredState = serde_json::from_value(old_json).unwrap();
        assert!(content_audit_valid(restored.stage_workflow.as_ref().unwrap().current()));

        let mut stage = restored.stage_workflow.as_ref().unwrap().current().clone();
        let edits = BTreeMap::from([("open_flags".into(), "人工旧草稿".into())]);
        let fields = HashSet::from(["open_flags".to_string()]);
        archive_content_brief_edits(&mut stage, &edits, &BTreeMap::new(), &fields,
            "root_request_changed");
        archive_content_brief_edits(&mut stage, &edits, &BTreeMap::new(), &fields,
            "root_request_changed");
        assert_eq!(stage.content_brief_edit_archives.len(), 1);
        assert!(content_audit_valid(&stage));

        let mut legacy = initial();
        let question: BrainstormQuestion = serde_json::from_value(content_question()).unwrap();
        legacy.stage_workflow.as_mut().unwrap().current_mut().questions.push(question.clone());
        legacy.turns.push(BrainstormStoredTurn {question:question.clone(),
            answer:BrainstormAnswer {selected_option_ids:vec!["a".into()],
                source:"user".into(), ..Default::default()}});
        legacy.brief_edits.insert(question.id.clone(), "v16 人工旧文案".into());
        let mut req = request("reopen", 1);
        req.question_id = Some(question.id.clone());
        assert_eq!(revise_saved_answer(&mut legacy, &req, 2).unwrap(), "exploring");
        assert!(legacy.stage_workflow.as_ref().unwrap().current()
            .content_brief_edit_archives.is_empty());
        assert!(!legacy.brief_edits.contains_key(&question.id));
    }
    fn ready() -> BrainstormStoredState {
        let mut stored = initial();
        apply_batch(&mut stored, plan()).unwrap();
        apply_batch(&mut stored, tree()).unwrap();
        stored
    }
    fn request(action: &str, revision: i64) -> BrainstormTurnRequest {
        serde_json::from_value(json!({"session_id":SESSION,"root_request":"撰写团队方案","workflow_version":2,"action":action,"revision":revision})).unwrap()
    }
    fn label_patch(
        question: &BrainstormQuestion,
        option_index: usize,
        text: &str,
    ) -> CandidateCopyPatch {
        CandidateCopyPatch {
            question_id: question.id.clone(),
            option_id: Some(question.options[option_index].id.clone()),
            field: CandidateCopyField::Label,
            expected_text: question.options[option_index].label.clone(),
            text: text.into(),
        }
    }
    fn why_patch(question: &BrainstormQuestion, text: &str) -> CandidateCopyPatch {
        CandidateCopyPatch {
            question_id: question.id.clone(),
            option_id: None,
            field: CandidateCopyField::WhyNow,
            expected_text: question.why_now.clone(),
            text: text.into(),
        }
    }

    fn preparing_topic() -> BrainstormStoredState {
        let mut stored = initial();
        apply_batch(&mut stored, plan()).unwrap();
        stored
    }

    #[test]
    fn finite_root_values_reject_free_copy_wrong_primitives_and_missing_proof_atomically() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["first property", "second property"]);
        let valid = node_output(&stored, json!([]));
        let mut variants = Vec::new();
        for field in [
            "prompt",
            "why_now",
            "single_choice_reason",
            "answer_template",
            "dimension",
            "dimension_id",
            "type",
        ] {
            let mut changed = valid.clone();
            changed["question"][field] = json!("把受众取舍捆绑为内容侧重或信息深度");
            variants.push(changed);
        }
        for field in ["label", "description", "id"] {
            let mut changed = valid.clone();
            changed["question"]["options"][0][field] = json!("附加其它表达策略");
            variants.push(changed);
        }
        for field in ["required", "allow_custom"] {
            let mut changed = valid.clone();
            changed["question"][field] = json!(false);
            variants.push(changed);
        }
        let mut missing = valid.clone();
        missing
            .as_object_mut()
            .unwrap()
            .remove("root_value_selection");
        variants.push(missing);
        for field in ["description", "label", "type", "next_goals"] {
            let mut changed = valid.clone();
            changed["root_value_selection"]["options"][0][field] = json!("额外自由文字");
            variants.push(changed);
        }
        for value_id in ["tone.objective", "unknown", "audience.participants+policy"] {
            let mut changed = valid.clone();
            changed["root_value_selection"]["options"][0]["value_id"] = json!(value_id);
            variants.push(changed);
        }
        let mut changed = valid.clone();
        changed["root_value_selection"]["options"][1]["value_id"] =
            changed["root_value_selection"]["options"][0]["value_id"].clone();
        variants.push(changed);
        for recommended in [json!(true), json!("false"), json!(0)] {
            let mut changed = valid.clone();
            changed["root_value_selection"]["options"][1]["recommended"] = recommended;
            variants.push(changed);
        }
        let mut changed = valid.clone();
        changed["root_value_selection"]["options"][0]["recommended"] = json!(false);
        variants.push(changed);
        let mut changed = valid.clone();
        changed["root_value_selection"]["options"][0]["memory_choice"] = json!("fabricated");
        variants.push(changed);
        let before = serde_json::to_value(&stored).unwrap();
        for (index, invalid) in variants.into_iter().enumerate() {
            assert!(
                apply_preparation_step(&mut stored, &request("status", 0), invalid).is_err(),
                "variant {index}"
            );
            assert_eq!(
                serde_json::to_value(&stored).unwrap(),
                before,
                "variant {index}"
            );
        }
        apply_preparation_step(&mut stored, &request("status", 0), valid).unwrap();
        assert_eq!(checkpoint(&stored).questions.len(), 1);
        assert_eq!(checkpoint(&stored).root_value_selections.len(), 1);
    }

    #[test]
    fn finite_root_ledger_restores_exact_value_copy_recommendation_ids_and_original_sources() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["first property", "second property"]);
        let mut output = node_output(&stored, json!([]));
        output["root_value_selection"]["options"][0]["recommended"] = json!(false);
        output["root_value_selection"]["options"][1]["recommended"] = json!(true);
        output["question"]["options"][0]["recommended"] = json!(false);
        output["question"]["options"][1]["recommended"] = json!(true);
        output["question"]["options"][0]["details"] = json!("来源甲原文保持");
        output["question"]["options"][1]["details"] = json!("来源乙原文保持");
        output["question"]["context_details"] = json!("完整原始来源编译提示");
        output["question"]["options"]
            .as_array_mut()
            .unwrap()
            .swap(0, 1);
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        let cp = checkpoint(&stored);
        assert_eq!(cp.questions[0].options[0].id, "b");
        assert_eq!(cp.questions[0].options[1].id, "a");
        assert_eq!(cp.questions[0].options[0].details, "来源乙原文保持");
        assert_eq!(cp.questions[0].options[1].details, "来源甲原文保持");
        assert_eq!(cp.questions[0].context_details, "完整原始来源编译提示");
        let raw = serde_json::to_value(&stored).unwrap();
        let restored: BrainstormStoredState = serde_json::from_value(raw.clone()).unwrap();
        assert!(batch_payload(&request("status", 0), &restored).is_ok());
        for change in ["missing", "wrong_node", "primitive", "copy", "type"] {
            let mut changed = restored.clone();
            let cp = changed
                .stage_workflow
                .as_mut()
                .unwrap()
                .current_mut()
                .topic_checkpoint
                .as_mut()
                .unwrap();
            match change {
                "missing" => cp.root_value_selections.clear(),
                "wrong_node" => cp.root_value_selections[0].node_id = "other-node".into(),
                "primitive" => {
                    cp.root_value_selections[0].selection["options"][0]["value_id"] =
                        json!("purpose.understand_state")
                }
                "copy" => cp.questions[0].options[0].description = "侧重指标内容并省略细节".into(),
                "type" => cp.questions[0].question_type = "single_choice".into(),
                _ => unreachable!(),
            }
            let before = serde_json::to_value(&changed).unwrap();
            assert!(
                batch_payload(&request("status", 0), &changed).is_err(),
                "{change}"
            );
            assert_eq!(serde_json::to_value(&changed).unwrap(), before);
        }
        let response =
            serde_json::to_value(brainstorm_response(SESSION, "preparing", 1, &restored)).unwrap();
        for internal in [
            "root_value_selections",
            "root_value_selection",
            "value_capabilities_sha256",
            "audience.participants",
        ] {
            assert!(!response.to_string().contains(internal));
        }
        assert_eq!(serde_json::to_value(&restored).unwrap(), raw);
    }

    #[test]
    fn atomic_properties_publish_one_terminal_question_each_with_original_sources_and_no_children()
    {
        let mut stored = initial();
        stored.stage_workflow.as_mut().unwrap().active_stage = "detail".into();
        stored.stage_workflow.as_mut().unwrap().current_mut().status = "thinking".into();
        let binding = root_axes::load().unwrap();
        let ids = binding
            .eligible("detail")
            .unwrap()
            .iter()
            .map(|axis| axis["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        let limits = BuildLimits::configured();
        let topics = binding
            .compile_topics(
                "detail",
                &ids,
                stage_limits()["max_topics"].as_u64().unwrap() as usize,
                limits.max_questions_per_topic,
            )
            .unwrap();
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            json!({"topics":topics,"chapters":[],"open_flags":[]}),
        )
        .unwrap();
        let mut node_count = 0;
        while stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .progress
            .completed_batches
            < stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .progress
                .total_batches
        {
            let payload = batch_payload(&request("status", 0), &stored).unwrap();
            assert_eq!(payload["root_limit"], limits.max_questions_per_topic);
            if payload["kind"] == "topic_plan" {
                apply_preparation_step(
                    &mut stored,
                    &request("status", 0),
                    compiled_topic_roots(&payload),
                )
                .unwrap();
                assert!(checkpoint(&stored).atomic_root_questions);
                continue;
            }
            let cp = checkpoint(&stored);
            let ordinal = cp.questions.len();
            assert_eq!(
                payload["node_mode"],
                json!({"kind":"root_axis_atomic","axis_id":cp.root_axis_ids[ordinal],"node_id":cp.frontier[0].id})
            );
            assert_eq!(payload["node"]["depth"], 1);
            assert_eq!(payload["node"]["max_options"], 5);
            assert_eq!(payload["node"]["max_next_goals"], 0);
            assert_eq!(payload["node"]["ancestor_conditions"], json!([]));
            let output = node_output(&stored, json!([]));
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
            node_count += 1;
        }
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(node_count, ids.len());
        assert_eq!(stage.questions.len(), ids.len());
        assert!(stage
            .questions
            .iter()
            .all(|q| q.parent_question_id.is_none() && q.parent_option_id.is_none()));
        assert!(stage
            .questions
            .iter()
            .all(|q| q.options[0].details == "已验证来源摘录：保持原文与身份"));
        assert_eq!(stored.memory_brief, "本主题已核验来源");
        assert!(stage.topic_checkpoint.is_none());
        assert_eq!(stage.status, "answering");
    }

    #[test]
    fn atomic_root_mode_cannot_be_self_granted_or_tampered_in_saved_checkpoint() {
        for change in [
            "mode_false",
            "mode_old_contract",
            "depth",
            "parent",
            "parent_option",
            "goal",
            "next_ordinal",
            "missing_root",
            "other_property",
            "registry_mode",
        ] {
            let mut stored = preparing_topic();
            begin_topic(&mut stored, &["fixture root"]);
            let output = node_output(&stored, json!([]));
            let cp = stored
                .stage_workflow
                .as_mut()
                .unwrap()
                .current_mut()
                .topic_checkpoint
                .as_mut()
                .unwrap();
            match change {
                "mode_false" => cp.atomic_root_questions = false,
                "mode_old_contract" => cp.preparation_contract_version = 14,
                "depth" => cp.frontier[0].depth = 2,
                "parent" => cp.frontier[0].parent_question_id = Some("untrusted-parent".into()),
                "parent_option" => cp.frontier[0].parent_option_id = Some("a".into()),
                "goal" => cp.frontier[0].goal = "其他属性的目标".into(),
                "next_ordinal" => cp.next_node_index += 1,
                "missing_root" => cp.root_axis_ids.clear(),
                "other_property" => cp.root_axis_ids[0] = "purpose".into(),
                "registry_mode" => {
                    cp.root_axes_binding.as_mut().unwrap().config["axes"][0]["decision_mode"] =
                        json!("tree")
                }
                _ => unreachable!(),
            }
            let before = serde_json::to_value(&stored).unwrap();
            assert!(
                batch_payload(&request("status", 0), &stored).is_err(),
                "{change}"
            );
            assert!(
                apply_preparation_step(&mut stored, &request("status", 0), output).is_err(),
                "{change}"
            );
            assert_eq!(serde_json::to_value(&stored).unwrap(), before, "{change}");
        }
    }

    #[test]
    fn atomic_root_rejects_child_goals_and_wrong_candidate_shape_without_changing_any_state() {
        for change in [
            "child",
            "empty_branch",
            "old_tree",
            "too_many_options",
            "fragment",
        ] {
            let mut stored = preparing_topic();
            begin_topic(&mut stored, &["fixture root"]);
            let mut output = node_output(&stored, json!([]));
            match change {
                "child" => {
                    output["next_goals"] =
                        json!([{"option_id":"a","goals":["新的不属于原属性的选择"]}])
                }
                "empty_branch" => output["next_goals"] = json!([{"option_id":"a","goals":[]}]),
                "old_tree" => output = tree(),
                "too_many_options" => {
                    for index in 2..6 {
                        output["question"]["options"].as_array_mut().unwrap().push(json!({"id":((b'a'+index) as char).to_string(),"label":format!("完整方案{index}"),"description":"另一项完整方案描述","recommended":false}));
                    }
                }
                "fragment" => output["question"]["options"][0]["label"] = json!("截止2026-08-"),
                _ => unreachable!(),
            }
            let before = serde_json::to_value(&stored).unwrap();
            assert!(
                apply_preparation_step(&mut stored, &request("status", 0), output).is_err(),
                "{change}"
            );
            assert_eq!(serde_json::to_value(&stored).unwrap(), before, "{change}");
        }
    }

    #[test]
    fn version14_unanswered_full_tree_checkpoint_is_archived_exact_without_new_false_mode_field() {
        let mut stored = preparing_topic();
        begin_legacy_topic(&mut stored, &["原注册阅读属性"]);
        let mut root = node_output(
            &stored,
            json!([
            {"option_id":"a","goals":["甲实施","甲核验"]},
            {"option_id":"b","goals":["乙实施","乙核验"]},
            {"option_id":"c","goals":["丙实施","丙核验"]},
            {"option_id":"d","goals":["丁实施","丁核验"]},
            {"option_id":"e","goals":["戊实施","戊核验"]}]),
        );
        root["question"]["type"] = json!("multi_choice");
        for index in 2..5 {
            root["question"]["options"].as_array_mut().unwrap().push(json!({
            "id":((b'a'+index) as char).to_string(),"label":format!("完整阅读范围{index}"),"description":"该阅读范围的完整说明","recommended":false}));
        }
        apply_preparation_step(&mut stored, &request("status", 0), root).unwrap();
        for _ in 0..3 {
            let leaf = node_output(&stored, json!([]));
            apply_preparation_step(&mut stored, &request("status", 0), leaf).unwrap();
        }
        // The generic tree was constructed as authentic protected v14 work.
        // This migration scenario is the unanswered state after that fixture
        // protection is explicitly removed; the old input identity is unchanged.
        stored.stage_workflow.as_mut().unwrap().chapters.clear();
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.status = "failed".into();
        stage.error = Some(
            recovery_exhausted(Some("BRAINSTORM_MODEL_OUTPUT_INVALID"))
                .1
                 .0,
        );
        stage.automatic_recovery.as_mut().unwrap().requests_started = 3;
        stage
            .automatic_recovery
            .as_mut()
            .unwrap()
            .stage_retries_used = 2;
        let before = serde_json::to_value(&stored).unwrap();
        let old_cp = before["stage_workflow"]["stages"][0]["topic_checkpoint"].clone();
        assert_eq!(old_cp["questions"].as_array().unwrap().len(), 4);
        assert_eq!(old_cp["frontier"].as_array().unwrap().len(), 7);
        assert!(old_cp.get("atomic_root_questions").is_none());
        assert!(refresh_generator_contract(&mut stored).unwrap());
        let after = serde_json::to_value(&stored).unwrap();
        let stage = &after["stage_workflow"]["stages"][0];
        assert_eq!(stage["topic_checkpoint_archives"][0]["checkpoint"], old_cp);
        for field in [
            "topics",
            "questions",
            "progress",
            "error",
            "automatic_recovery",
            "root_axes_binding",
        ] {
            assert_eq!(
                stage["preparation_archives"][0][field],
                before["stage_workflow"]["stages"][0][field],
                "{field}"
            );
        }
        for field in [
            "root_request",
            "selected_skills",
            "turns",
            "brief_edits",
            "user_input_revisions",
            "memory_brief",
        ] {
            assert_eq!(after[field], before[field], "{field}");
        }
        assert_eq!(
            after["stage_workflow"]["stages"][1],
            before["stage_workflow"]["stages"][1]
        );
        assert_eq!(
            after["stage_workflow"]["stages"][2],
            before["stage_workflow"]["stages"][2]
        );
        assert!(stage["topics"].as_array().unwrap().is_empty());
        assert!(stage.get("topic_checkpoint").is_none());
        assert!(!refresh_generator_contract(&mut stored).unwrap());
    }

    #[test]
    fn version15_atomic_pool_and_checkpoint_archive_exact_before_finite_renderer_replan() {
        let mut stored = initial();
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        let mut plan = compiled_plan_for_request(&payload);
        let binding = root_axes::load().unwrap();
        plan["topics"] = json!(binding
            .compile_topics(
                "direction",
                &["audience".into(), "purpose".into(), "content_focus".into()],
                3,
                12
            )
            .unwrap());
        apply_preparation_step(&mut stored, &request("status", 0), plan).unwrap();
        for _ in 0..2 {
            let payload = batch_payload(&request("status", 0), &stored).unwrap();
            apply_preparation_step(
                &mut stored,
                &request("status", 0),
                compiled_topic_roots(&payload),
            )
            .unwrap();
            let output = node_output(&stored, json!([]));
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        }
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            compiled_topic_roots(&payload),
        )
        .unwrap();
        let old_bytes = include_bytes!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/brainstorm-root-axes-v15.json"
        ));
        let old_binding = RootAxesBinding {
            config_sha256: sha256_hex(std::str::from_utf8(old_bytes).unwrap()),
            config: serde_json::from_slice(old_bytes).unwrap(),
        };
        assert_eq!(
            old_binding.config_sha256,
            "f984c6e7d2a9d07044bd1af6e7f19453a9c54903db6cb48ab7372be3a6bb17f0"
        );
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = 15;
        stage.root_axes_binding = Some(old_binding.clone());
        stage.questions[0].options[0].description = "旧候选捆绑阅读对象与表达侧重".into();
        stage.status = "failed".into();
        stage.error = Some(
            recovery_exhausted(Some("BRAINSTORM_MODEL_OUTPUT_INVALID"))
                .1
                 .0,
        );
        let cp = stage.topic_checkpoint.as_mut().unwrap();
        cp.preparation_contract_version = 15;
        cp.root_axes_binding = Some(old_binding);
        assert!(cp.atomic_root_questions);
        assert!(cp.root_value_selections.is_empty());
        let mut prior = cp.clone();
        prior.preparation_contract_version = 14;
        prior.atomic_root_questions = false;
        stage
            .topic_checkpoint_archives
            .push(TopicCheckpointArchive {
                reason: "contract_changed".into(),
                archived_at_ms: 1,
                checkpoint: prior,
            });
        enable_recovery(&mut stored, 3, 2);
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        let recovery = stage.automatic_recovery.as_mut().unwrap();
        recovery.requests_started = 3;
        recovery.stage_retries_used = 2;
        recovery.last_failure_code = Some("BRAINSTORM_MODEL_OUTPUT_INVALID".into());
        let before = serde_json::to_value(&stored).unwrap();
        let old = &before["stage_workflow"]["stages"][0];
        assert_eq!(old["questions"].as_array().unwrap().len(), 2);
        assert_eq!(old["topic_checkpoint"]["questions"], json!([]));
        assert_eq!(
            old["topic_checkpoint"]["frontier"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(old["topic_checkpoint"]["atomic_root_questions"], true);
        assert!(old["topic_checkpoint"]
            .get("root_value_selections")
            .is_none());
        assert!(refresh_generator_contract(&mut stored).unwrap());
        let after = serde_json::to_value(&stored).unwrap();
        let stage = &after["stage_workflow"]["stages"][0];
        assert_eq!(
            stage["topic_checkpoint_archives"][0],
            old["topic_checkpoint_archives"][0]
        );
        assert_eq!(
            stage["topic_checkpoint_archives"][1]["checkpoint"],
            old["topic_checkpoint"]
        );
        for field in [
            "topics",
            "questions",
            "progress",
            "root_axes_binding",
            "error",
            "automatic_recovery",
        ] {
            assert_eq!(
                stage["preparation_archives"][0][field], old[field],
                "{field}"
            );
        }
        for field in [
            "root_request",
            "selected_skills",
            "turns",
            "brief_edits",
            "user_input_revisions",
            "memory_brief",
        ] {
            assert_eq!(after[field], before[field]);
        }
        assert_eq!(
            after["stage_workflow"]["stages"][1],
            before["stage_workflow"]["stages"][1]
        );
        assert_eq!(
            after["stage_workflow"]["stages"][2],
            before["stage_workflow"]["stages"][2]
        );
        assert!(stage["topics"].as_array().unwrap().is_empty());
        assert!(stage["questions"].as_array().unwrap().is_empty());
        assert!(stage.get("topic_checkpoint").is_none());
        assert!(!refresh_generator_contract(&mut stored).unwrap());
        assert!(refresh_root_axes_binding(&mut stored).unwrap());
        assert_eq!(
            batch_payload(&request("status", 0), &stored).unwrap()["kind"],
            "plan"
        );
    }

    #[test]
    fn protected_version15_atomic_checkpoint_cannot_fall_back_to_legacy_tree() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["one property"]);
        let bytes = include_bytes!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/brainstorm-root-axes-v15.json"
        ));
        let old = RootAxesBinding {
            config_sha256: sha256_hex(std::str::from_utf8(bytes).unwrap()),
            config: serde_json::from_slice(bytes).unwrap(),
        };
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.chapters.push(Chapter {
            id: "old-body".into(),
            title: "原正文".into(),
            goal: "原内容".into(),
            status: "completed".into(),
            question_ids: vec![],
            instruction_id: Some("old-instruction".into()),
            operation_id: Some("old-operation".into()),
        });
        let stage = workflow.current_mut();
        stage.preparation_contract_version = 15;
        stage.root_axes_binding = Some(old.clone());
        let cp = stage.topic_checkpoint.as_mut().unwrap();
        cp.preparation_contract_version = 15;
        cp.root_axes_binding = Some(old);
        let fingerprint = input_fingerprint(&request("status", 0), &stored);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .input_fingerprint = fingerprint;
        let before = serde_json::to_value(&stored).unwrap();
        assert!(checkpoint(&stored).atomic_root_questions);
        assert!(!refresh_generator_contract(&mut stored).unwrap());
        assert!(!refresh_root_axes_binding(&mut stored).unwrap());
        assert!(!refresh_checkpoint_input(
            &mut stored,
            &request("status", 0)
        ));
        let failure = batch_payload(&request("status", 0), &stored).unwrap_err();
        assert_eq!(failure.1 .0["code"], "BRAINSTORM_STAGE_CONFLICT");
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        assert_eq!(
            before["stage_workflow"]["stages"][0]["topic_checkpoint"]["atomic_root_questions"],
            true
        );
        assert!(before["stage_workflow"]["stages"][0]["topic_checkpoint"]
            .get("root_value_selections")
            .is_none());
    }

    #[test]
    fn protected_version14_checkpoint_keeps_actual_manifest_identity_and_uneven_tree_budget() {
        let mut stored = preparing_topic();
        begin_legacy_topic(&mut stored, &["第一原属性", "第二原属性"]);
        let before = serde_json::to_value(&stored).unwrap();
        assert!(!refresh_generator_contract(&mut stored).unwrap());
        assert!(!refresh_root_axes_binding(&mut stored).unwrap());
        assert!(!refresh_checkpoint_input(
            &mut stored,
            &request("status", 0)
        ));
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        assert!(payload.get("root_axes_binding").is_none());
        assert!(payload.get("node_mode").is_none());
        assert!(payload["node"]["max_next_goals"].as_u64().unwrap() >= 3);
        let output = node_output(
            &stored,
            json!([
            {"option_id":"a","goals":["甲执行","甲核验"]},{"option_id":"b","goals":["乙执行"]}]),
        );
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        assert_eq!(checkpoint(&stored).preparation_contract_version, 14);
        assert!(!checkpoint(&stored).atomic_root_questions);
        assert_eq!(checkpoint(&stored).questions.len(), 1);
        assert_eq!(checkpoint(&stored).frontier.len(), 4);
        assert_eq!(
            serde_json::to_value(&stored.stage_workflow.as_ref().unwrap().chapters).unwrap(),
            before["stage_workflow"]["chapters"]
        );
    }

    #[test]
    fn current_free_network_rejects_valid_legacy_complete_tree_without_mutating_state() {
        for stage_name in ["direction", "detail"] {
            let mut stored = initial();
            stored.stage_workflow.as_mut().unwrap().active_stage = stage_name.into();
            stored.stage_workflow.as_mut().unwrap().current_mut().status = "thinking".into();
            let payload = batch_payload(&request("status", 0), &stored).unwrap();
            apply_preparation_step(
                &mut stored,
                &request("status", 0),
                compiled_plan_for_request(&payload),
            )
            .unwrap();
            let legacy_output = tree();
            assert_eq!(legacy_output["questions"].as_array().unwrap().len(), 3);
            // The tree is complete and otherwise valid under the retained local adapter.
            let mut local_adapter = stored.clone();
            assert_eq!(
                apply_batch(&mut local_adapter, legacy_output.clone()).unwrap(),
                "exploring"
            );
            assert_eq!(
                local_adapter
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .current()
                    .questions
                    .len(),
                3
            );
            let before = serde_json::to_value(&stored).unwrap();
            let failure = apply_preparation_step(&mut stored, &request("status", 0), legacy_output)
                .unwrap_err();
            assert_eq!(failure.1 .0["code"], "BRAINSTORM_MODEL_OUTPUT_INVALID");
            assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        }
    }

    #[test]
    fn compiled_free_manifest_covers_all_selected_attributes_with_exact_topic_roots() {
        for stage_name in ["direction", "detail"] {
            let mut stored = initial();
            let workflow = stored.stage_workflow.as_mut().unwrap();
            workflow.active_stage = stage_name.into();
            workflow.current_mut().status = "thinking".into();
            let binding = workflow
                .current()
                .root_axes_binding
                .as_ref()
                .unwrap()
                .clone();
            let ids = binding
                .eligible(stage_name)
                .unwrap()
                .iter()
                .map(|axis| axis["id"].as_str().unwrap().to_owned())
                .collect::<Vec<_>>();
            let topics = binding.compile_topics(stage_name, &ids, 6, 4).unwrap();
            let output = json!({"topics":topics,"chapters":[],"open_flags":[]});
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
            let workflow = stored.stage_workflow.as_ref().unwrap();
            let saved_ids = workflow
                .current()
                .topics
                .iter()
                .flat_map(|topic| topic.root_axis_ids.clone())
                .collect::<Vec<_>>();
            assert_eq!(saved_ids, ids);
            let payload = batch_payload(&request("status", 0), &stored).unwrap();
            let roots = compiled_topic_roots(&payload);
            let expected_ids = workflow.current().topics[0].root_axis_ids.clone();
            apply_preparation_step(&mut stored, &request("status", 0), roots).unwrap();
            assert_eq!(checkpoint(&stored).root_axis_ids, expected_ids);
            let public =
                serde_json::to_string(&stored.stage_workflow.as_ref().unwrap().for_response())
                    .unwrap();
            assert!(!public.contains("root_axis_ids"));
            assert!(!public.contains("root_axes_binding"));
        }
    }

    #[test]
    fn compiled_free_manifest_rejects_fake_scope_duplicate_mixed_or_missing_proof_atomically() {
        for change in [
            "id",
            "title",
            "goal",
            "extra",
            "missing",
            "duplicate",
            "cross_stage",
            "flags",
            "memory",
            "chapters",
        ] {
            let mut stored = initial();
            let mut output = compiled_plan_for_request(&json!({"stage":"direction"}));
            match change {
                "id" => output["topics"][0]["id"] = json!("fake_topic"),
                "title" => output["topics"][0]["title"] = json!("统计周期"),
                "goal" => output["topics"][0]["goal"] = json!("从旧报告继承本次统计周期"),
                "extra" => output["topics"][0]["fake_id"] = json!("audience"),
                "missing" => {
                    output["topics"][0]
                        .as_object_mut()
                        .unwrap()
                        .remove("root_axis_ids");
                }
                "duplicate" => {
                    let duplicate = output["topics"][0].clone();
                    output["topics"].as_array_mut().unwrap().push(duplicate);
                }
                "cross_stage" => {
                    output["topics"][0]["root_axis_ids"] = json!(["information_granularity"])
                }
                "flags" => output["open_flags"] = json!(["模型新增待确认事实"]),
                "memory" => output["memory_brief"] = json!("模型新增历史前提"),
                "chapters" => {
                    output["chapters"] = json!([{"id":"fake","title":"fake","goal":"fake"}])
                }
                _ => unreachable!(),
            }
            let before = serde_json::to_value(&stored).unwrap();
            assert!(
                apply_preparation_step(&mut stored, &request("status", 0), output).is_err(),
                "{change}"
            );
            assert_eq!(serde_json::to_value(&stored).unwrap(), before, "{change}");
        }
    }

    #[test]
    fn compiled_topic_roots_cannot_add_other_legal_registry_attributes() {
        let mut stored = initial();
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            compiled_plan_for_request(&json!({"stage":"direction"})),
        )
        .unwrap();
        let binding = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .root_axes_binding
            .as_ref()
            .unwrap();
        let purpose = binding
            .eligible("direction")
            .unwrap()
            .into_iter()
            .find(|axis| axis["id"] == "purpose")
            .unwrap()["goal"]
            .clone();
        let before = serde_json::to_value(&stored).unwrap();
        assert!(apply_preparation_step(
            &mut stored,
            &request("status", 0),
            json!({"root_goals":[purpose]})
        )
        .is_err());
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            compiled_topic_roots(&payload),
        )
        .unwrap();
        assert_eq!(checkpoint(&stored).root_axis_ids, vec!["audience"]);
    }

    #[test]
    fn compiled_plan_keeps_user_flags_original_lines_and_manifest_changes_invalidate_identity() {
        let mut stored = initial();
        stored.brief_edits.insert(
            "open_flags".into(),
            "  第一项  \r\n\u{001f}\n第二项\u{2028}第三项".into(),
        );
        let body = json!({"stage":"direction","brief_edits":stored.brief_edits});
        let output = compiled_plan_for_request(&body);
        assert_eq!(
            output["open_flags"],
            json!(["  第一项  ", "第二项", "第三项"])
        );
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        assert_eq!(stored.open_flags, vec!["  第一项  ", "第二项", "第三项"]);
        let old = input_fingerprint(&request("status", 0), &stored);
        stored.stage_workflow.as_mut().unwrap().current_mut().topics[0]
            .goal
            .push_str("变更");
        assert_ne!(old, input_fingerprint(&request("status", 0), &stored));
    }

    #[test]
    fn root_axes_binding_is_internal_and_sent_for_every_free_preparation_kind() {
        let req = request("status", 0);
        let mut stored = initial();
        let binding = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .root_axes_binding
            .clone()
            .unwrap();
        assert_eq!(
            batch_payload(&req, &stored).unwrap()["root_axes_binding"],
            serde_json::to_value(&binding).unwrap()
        );
        apply_batch(&mut stored, plan()).unwrap();
        assert_eq!(batch_payload(&req, &stored).unwrap()["kind"], "topic_plan");
        begin_topic(&mut stored, &["fixture root"]);
        let cp = checkpoint(&stored);
        assert_eq!(cp.root_axis_ids, vec!["audience"]);
        assert_eq!(cp.root_axes_binding, Some(binding.clone()));
        assert_eq!(batch_payload(&req, &stored).unwrap()["kind"], "node");
        let public =
            serde_json::to_string(&stored.stage_workflow.as_ref().unwrap().for_response()).unwrap();
        let brief = brainstorm_brief(&stored);
        for value in [public, brief] {
            assert!(!value.contains("root_axes_binding"));
            assert!(!value.contains("root_axis_ids"));
            assert!(!value.contains(&binding.config_sha256));
            assert!(!value.contains("generic-creation-attributes"));
        }
        let (catalog, _) = catalog_initial();
        assert!(batch_payload(&req, &catalog)
            .unwrap()
            .get("root_axes_binding")
            .is_none());
        stored.stage_workflow.as_mut().unwrap().active_stage = "writing".into();
        assert!(batch_payload(&req, &stored)
            .unwrap()
            .get("root_axes_binding")
            .is_none());
    }

    #[test]
    fn root_axes_config_drift_archives_complete_unanswered_pool_and_checkpoint() {
        let mut stored = ready();
        stored.current_question = None;
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.status = "thinking".into();
        stage.progress.completed_batches = 1;
        begin_topic(&mut stored, &["fixture root"]);
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.root_axes_binding.as_mut().unwrap().config_sha256 = "old-config-sha".into();
        stage.topic_checkpoint.as_mut().unwrap().root_axes_binding =
            stage.root_axes_binding.clone();
        stage.error = Some(json!({"code":"BRAINSTORM_MODEL_OUTPUT_INVALID","retryable":true}));
        let before = serde_json::to_value(&stored).unwrap();
        let old_fingerprint = input_fingerprint(&request("status", 0), &stored);
        assert!(refresh_root_axes_binding(&mut stored).unwrap());
        let after = serde_json::to_value(&stored).unwrap();
        let old = &before["stage_workflow"]["stages"][0];
        let stage = &after["stage_workflow"]["stages"][0];
        assert_eq!(
            stage["preparation_archives"][0]["questions"],
            old["questions"]
        );
        assert_eq!(stage["preparation_archives"][0]["topics"], old["topics"]);
        assert_eq!(
            stage["preparation_archives"][0]["progress"],
            old["progress"]
        );
        assert_eq!(stage["preparation_archives"][0]["error"], old["error"]);
        assert_eq!(
            stage["preparation_archives"][0]["root_axes_binding"],
            old["root_axes_binding"]
        );
        assert_eq!(
            stage["topic_checkpoint_archives"][0]["checkpoint"],
            old["topic_checkpoint"]
        );
        for field in [
            "root_request",
            "selected_skills",
            "turns",
            "brief_edits",
            "user_input_revisions",
            "memory_brief",
        ] {
            assert_eq!(after[field], before[field]);
        }
        for index in 1..3 {
            assert_eq!(
                after["stage_workflow"]["stages"][index],
                before["stage_workflow"]["stages"][index]
            );
        }
        assert_eq!(stage["topics"], json!([]));
        assert_eq!(stage["questions"], json!([]));
        assert_eq!(stage["progress"]["completed_batches"], 0);
        assert_eq!(
            batch_payload(&request("status", 0), &stored).unwrap()["kind"],
            "plan"
        );
        assert_ne!(
            old_fingerprint,
            input_fingerprint(&request("status", 0), &stored)
        );
        assert!(!refresh_root_axes_binding(&mut stored).unwrap());
    }

    #[test]
    fn root_axes_drift_preserves_answers_edits_body_operations_and_copy_repairs() {
        for protection in ["answer", "edit", "body", "operation", "copy"] {
            let mut stored = preparing_topic();
            begin_topic(&mut stored, &["fixture root"]);
            apply_batch(&mut stored, tree()).unwrap();
            let question = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
            match protection {
                "answer" => stored.turns.push(BrainstormStoredTurn {
                    question: question.clone(),
                    answer: BrainstormAnswer {
                        source: "user".into(),
                        selected_option_ids: vec!["a".into()],
                        ..Default::default()
                    },
                }),
                "edit" => {
                    stored
                        .brief_edits
                        .insert(question.id.clone(), "用户保留修订".into());
                }
                "copy" => stored
                    .stage_workflow
                    .as_mut()
                    .unwrap()
                    .copy_repair_history
                    .push(CandidateCopyRepair {
                        revision: 3,
                        question_id: question.id.clone(),
                        option_id: None,
                        field: CandidateCopyField::WhyNow,
                        old_text: "old".into(),
                        new_text: "new".into(),
                    }),
                _ => {
                    let mut chapter: Chapter = serde_json::from_value(json!({"id":"protected","title":"保留章节","goal":"已确认正文","status":"completed","question_ids":[],"content":"保留正文"})).unwrap();
                    if protection == "operation" {
                        chapter.operation_id = Some("protected-operation".into());
                    }
                    stored
                        .stage_workflow
                        .as_mut()
                        .unwrap()
                        .chapters
                        .push(chapter);
                }
            }
            let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
            stage.root_axes_binding.as_mut().unwrap().config_sha256 = "old-config-sha".into();
            let before = serde_json::to_value(&stored).unwrap();
            assert!(
                !refresh_root_axes_binding(&mut stored).unwrap(),
                "{protection}"
            );
            assert_eq!(
                serde_json::to_value(&stored).unwrap(),
                before,
                "{protection}"
            );
            let failure = batch_payload(&request("status", 0), &stored).unwrap_err();
            assert_eq!(failure.1 .0["code"], "BRAINSTORM_STAGE_CONFLICT");
            assert_eq!(
                serde_json::to_value(&stored).unwrap(),
                before,
                "{protection}"
            );
        }
    }

    #[test]
    fn protected_v12_root_checkpoint_continues_without_adopting_new_registry() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["fixture root"]);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .questions = serde_json::from_value(tree()["questions"].clone()).unwrap();
        let question = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
        stored.turns.push(BrainstormStoredTurn {
            question,
            answer: BrainstormAnswer {
                source: "user".into(),
                selected_option_ids: vec!["a".into()],
                ..Default::default()
            },
        });
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = 12;
        stage.root_axes_binding = None;
        let cp = stage.topic_checkpoint.as_mut().unwrap();
        cp.preparation_contract_version = 12;
        cp.atomic_root_questions = false;
        cp.root_axes_binding = None;
        cp.root_axis_ids.clear();
        cp.frontier[0].goal = "旧记录尚未完成的可信目标".into();
        let req = request("status", 0);
        let fingerprint = input_fingerprint(&req, &stored);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .input_fingerprint = fingerprint;
        let before = serde_json::to_value(&stored).unwrap();
        assert!(!refresh_generator_contract(&mut stored).unwrap());
        assert!(!refresh_root_axes_binding(&mut stored).unwrap());
        assert!(!refresh_checkpoint_input(&mut stored, &req));
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        let payload = batch_payload(&req, &stored).unwrap();
        assert_eq!(payload["node"]["goal"], "旧记录尚未完成的可信目标");
        assert!(payload.get("root_axes_binding").is_none());
        let output = node_output(
            &stored,
            json!([{"option_id":"a","goals":["旧目标甲细化"]},{"option_id":"b","goals":["旧目标乙细化"]}]),
        );
        apply_preparation_step(&mut stored, &req, output).unwrap();
        assert_eq!(checkpoint(&stored).questions.len(), 1);
        assert_eq!(checkpoint(&stored).preparation_contract_version, 12);
        assert!(stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .preparation_archives
            .is_empty());
    }

    #[test]
    fn version13_failed_free_manifest_checkpoint_and_old_budget_are_archived_whole_before14() {
        let mut stored = initial();
        let topics = (0..6)
            .map(|index| {
                json!({"id":format!("old-{index}"),
            "title":format!("旧主题{index}"),"goal":format!("旧主题目标{index}")})
            })
            .collect::<Vec<_>>();
        apply_batch(&mut stored, json!({"topics":topics,"chapters":[]})).unwrap();
        begin_topic(
            &mut stored,
            &[
                "old audience",
                "old purpose",
                "old focus",
                "old perspective",
            ],
        );
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = 13;
        stage.status = "failed".into();
        stage.error = Some(
            recovery_exhausted(Some("BRAINSTORM_MODEL_OUTPUT_INVALID"))
                .1
                 .0,
        );
        let recovery = stage.automatic_recovery.as_mut().unwrap();
        recovery.requests_started = 3;
        recovery.stage_retries_used = 2;
        recovery.last_failure_code = Some("BRAINSTORM_MODEL_OUTPUT_INVALID".into());
        let cp = stage.topic_checkpoint.as_mut().unwrap();
        cp.preparation_contract_version = 13;
        cp.input_fingerprint = "original-v13-authority-fingerprint".into();
        cp.topic.root_axis_ids.clear();
        for topic in &mut stage.topics {
            topic.root_axis_ids.clear();
        }
        let before = serde_json::to_value(&stored).unwrap();
        assert!(refresh_generator_contract(&mut stored).unwrap());
        let after = serde_json::to_value(&stored).unwrap();
        let old_stage = &before["stage_workflow"]["stages"][0];
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        let archive = serde_json::to_value(&stage.preparation_archives[0]).unwrap();
        for field in [
            "topics",
            "questions",
            "progress",
            "error",
            "automatic_recovery",
            "root_axes_binding",
        ] {
            assert_eq!(archive[field], old_stage[field], "{field}");
        }
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[0].checkpoint).unwrap(),
            old_stage["topic_checkpoint"]
        );
        for field in [
            "root_request",
            "selected_skills",
            "turns",
            "brief_edits",
            "user_input_revisions",
            "memory_brief",
        ] {
            assert_eq!(after[field], before[field], "{field}");
        }
        assert_eq!(
            after["stage_workflow"]["stages"][1],
            before["stage_workflow"]["stages"][1]
        );
        assert_eq!(
            after["stage_workflow"]["stages"][2],
            before["stage_workflow"]["stages"][2]
        );
        assert!(stage.topics.is_empty());
        assert!(stage.topic_checkpoint.is_none());
        assert!(stage.automatic_recovery.is_none());
        assert!(!refresh_generator_contract(&mut stored).unwrap());
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            compiled_plan_for_request(&payload),
        )
        .unwrap();
        assert_eq!(
            stored.stage_workflow.as_ref().unwrap().current().topics[0].root_axis_ids,
            ["audience"]
        );
    }

    #[test]
    fn protected_v13_root_checkpoint_keeps_original_registry_and_input_identity_for_node() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["fixture root"]);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .questions = serde_json::from_value(tree()["questions"].clone()).unwrap();
        let question = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
        stored.turns.push(BrainstormStoredTurn {
            question,
            answer: BrainstormAnswer {
                source: "user".into(),
                selected_option_ids: vec!["a".into()],
                ..Default::default()
            },
        });
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = 13;
        let binding = stage.root_axes_binding.as_mut().unwrap();
        binding.config_sha256 =
            "bd92600a517528fcd159ec67dbb3d9157064f0fd1a5cee61574c5c35762acff9".into();
        binding.config["registry_version"] = json!(1);
        for axis in binding.config["axes"].as_array_mut().unwrap() {
            axis.as_object_mut().unwrap().remove("title");
            axis.as_object_mut().unwrap().remove("decision_mode");
        }
        let old_binding = stage.root_axes_binding.clone();
        for topic in &mut stage.topics {
            topic.root_axis_ids.clear();
        }
        let cp = stage.topic_checkpoint.as_mut().unwrap();
        cp.preparation_contract_version = 13;
        cp.atomic_root_questions = false;
        cp.root_axes_binding = old_binding;
        cp.topic.root_axis_ids.clear();
        cp.frontier[0].goal = "旧受保护根的当前可信目标".into();
        let req = request("status", 0);
        // Reconstruct the actual version13 identity envelope explicitly:
        // neither version14 nor compiled_topic_manifest may enter old identity.
        let current = stored.stage_workflow.as_ref().unwrap().current();
        let mut original_inputs = json!({
            "session_id":SESSION,"stage":"direction","preparation_contract_version":13,
            "root_request":stored.root_request,"decisions":brainstorm_decision_context(&stored),
            "brief_edits":stored.brief_edits,"user_input_revisions":stored.user_input_revisions,
            "selected_skills":stored.selected_skills,"limits":stage_limits(),
            "catalog_binding":current.catalog_binding,"root_axes_binding":current.root_axes_binding,
        });
        if let Some(facts) = brainstorm_work_facts(&stored) {
            original_inputs["work_facts"] = facts;
        }
        let fingerprint = sha256_hex(&original_inputs.to_string());
        assert_eq!(input_fingerprint(&req, &stored), fingerprint);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .input_fingerprint = fingerprint;
        let before = serde_json::to_value(&stored).unwrap();
        assert!(!refresh_generator_contract(&mut stored).unwrap());
        assert!(!refresh_root_axes_binding(&mut stored).unwrap());
        assert!(!refresh_checkpoint_input(&mut stored, &req));
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        let payload = batch_payload(&req, &stored).unwrap();
        assert!(payload.get("root_axes_binding").is_none());
        assert_eq!(payload["node"]["goal"], "旧受保护根的当前可信目标");
        let output = node_output(
            &stored,
            json!([{"option_id":"a","goals":["旧根甲细化"]},{"option_id":"b","goals":["旧根乙细化"]}]),
        );
        apply_preparation_step(&mut stored, &req, output).unwrap();
        assert_eq!(checkpoint(&stored).questions.len(), 1);
        assert_eq!(checkpoint(&stored).preparation_contract_version, 13);
        assert!(stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .preparation_archives
            .is_empty());
    }

    #[test]
    fn selected_root_axis_proof_and_goal_cannot_be_tampered_before_node_save() {
        for change in ["unknown", "duplicate", "binding", "goal"] {
            let mut stored = preparing_topic();
            begin_topic(&mut stored, &["fixture root", "fixture second root"]);
            let output = node_output(&stored, json!([]));
            let cp = stored
                .stage_workflow
                .as_mut()
                .unwrap()
                .current_mut()
                .topic_checkpoint
                .as_mut()
                .unwrap();
            match change {
                "unknown" => cp.root_axis_ids[0] = "unknown".into(),
                "duplicate" => cp.root_axis_ids[1] = cp.root_axis_ids[0].clone(),
                "binding" => cp.root_axes_binding = None,
                "goal" => cp.frontier[0].goal = "模型改写根目标".into(),
                _ => unreachable!(),
            }
            let before = serde_json::to_value(&stored).unwrap();
            assert!(
                apply_preparation_step(&mut stored, &request("status", 0), output).is_err(),
                "{change}"
            );
            assert_eq!(serde_json::to_value(&stored).unwrap(), before, "{change}");
        }
    }

    #[test]
    fn root_axes_unknown_duplicate_or_wrong_stage_goals_cannot_change_saved_state() {
        let canonical = canonical_root_goals("direction", 1)[0].clone();
        let detail_only = root_axes::load()
            .unwrap()
            .eligible("detail")
            .unwrap()
            .into_iter()
            .find(|axis| axis["id"] == "information_granularity")
            .unwrap()["goal"]
            .as_str()
            .unwrap()
            .to_owned();
        for value in [
            json!({"root_goals":["unknown goal"]}),
            json!({"root_goals":[canonical,canonical]}),
            json!({"root_goals":[detail_only]}),
            json!({"root_goals":[canonical],"root_axis_ids":["audience"]}),
        ] {
            let mut stored = preparing_topic();
            let before = serde_json::to_value(&stored).unwrap();
            assert!(apply_preparation_step(&mut stored, &request("status", 0), value).is_err());
            assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        }
    }

    #[test]
    fn writing_payload_keeps_complete_chapter_scopes_without_execution_metadata() {
        let req = request("status", 0);
        let mut stored = initial();
        stored.stage_workflow.as_mut().unwrap().active_stage = "writing".into();
        let payload = batch_payload(&req, &stored).unwrap();
        assert_eq!(payload["kind"], "plan");
        assert_eq!(payload["plan"]["chapters"], json!([]));
        apply_batch(
            &mut stored,
            json!({
                "topics":[
                    {"id":"topic-one","title":"比较安排","goal":"比较组织","chapter_id":"one"},
                    {"id":"topic-two","title":"结论安排","goal":"结论组织","chapter_id":"two"}],
                "chapters":[
                    {"id":"one","title":"证据比较","goal":"仅比较已确认的两组证据"},
                    {"id":"two","title":"结论表达","goal":"仅表达已确认的结论与边界"}]
            }),
        )
        .unwrap();
        let expected = json!([
            {"id":"one","title":"证据比较","goal":"仅比较已确认的两组证据"},
            {"id":"two","title":"结论表达","goal":"仅表达已确认的结论与边界"}
        ]);
        let chapter = &mut stored.stage_workflow.as_mut().unwrap().chapters[0];
        chapter.instruction_id = Some("PRIVATE_INSTRUCTION_ID".into());
        chapter.operation_id = Some("PRIVATE_OPERATION_ID".into());
        chapter.status = "PRIVATE_EXECUTION_STATUS".into();
        let before = serde_json::to_value(&stored).unwrap();
        let payload = batch_payload(&req, &stored).unwrap();
        assert_eq!(payload["kind"], "topic_plan");
        assert_eq!(payload["topic"]["chapter_id"], "one");
        assert_eq!(payload["plan"]["chapters"], expected);
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        begin_topic(&mut stored, &["比较组织"]);
        let before = serde_json::to_value(&stored).unwrap();
        let payload = batch_payload(&req, &stored).unwrap();
        assert_eq!(payload["kind"], "node");
        assert_eq!(payload["topic"]["chapter_id"], "one");
        assert_eq!(payload["plan"]["chapters"], expected);
        for chapter in payload["plan"]["chapters"].as_array().unwrap() {
            assert_eq!(chapter.as_object().unwrap().len(), 3);
            assert!(chapter.get("instruction_id").is_none());
            assert!(chapter.get("operation_id").is_none());
            assert!(chapter.get("status").is_none());
        }
        assert!(!payload.to_string().contains("PRIVATE_"));
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
    }

    #[test]
    fn chapter_scope_payload_does_not_change_nonwriting_or_catalog_paths() {
        let req = request("status", 0);
        let stored = initial();
        assert!(batch_payload(&req, &stored).unwrap()["plan"]
            .get("chapters")
            .is_none());
        let (mut stored, _) = catalog_initial();
        assert!(batch_payload(&req, &stored).unwrap()["plan"]
            .get("chapters")
            .is_none());
        let mut binding = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .catalog_binding
            .as_ref()
            .unwrap()
            .clone();
        binding
            .catalog
            .stages
            .insert("writing".into(), binding.catalog.stages["detail"].clone());
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "writing".into();
        workflow.current_mut().catalog_binding = Some(binding);
        let before = serde_json::to_value(&stored).unwrap();
        let payload = batch_payload(&req, &stored).unwrap();
        assert!(payload["trusted_catalog"].is_object());
        assert!(payload["plan"].get("chapters").is_none());
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
    }

    fn legacy_detail_pool() -> BrainstormStoredState {
        let mut stored = ready();
        let root = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: root,
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: "保留用户原话".into(),
                source: "user".into(),
            },
        });
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.current_mut().status = "completed".into();
        workflow.active_stage = "detail".into();
        workflow.current_mut().status = "thinking".into();
        apply_batch(&mut stored, json!({"topics":(1..=5).map(|index| json!({"id":format!("topic{index}"),"title":format!("主题{index}"),"goal":format!("只落实主题{index}具体目标")})).collect::<Vec<_>>() })).unwrap();
        for index in 1..=4 {
            let mut tree = four_question_tree();
            for question in tree["questions"].as_array_mut().unwrap() {
                question["prompt"] = json!(format!(
                    "已存主题{index}：{}",
                    question["prompt"].as_str().unwrap()
                ));
                question["context_details"] = json!("PRIVATE_LONG_SOURCE_BODY");
                for option in question["options"].as_array_mut().unwrap() {
                    option["details"] = json!("PRIVATE_LONG_SOURCE_BODY");
                }
            }
            apply_batch(&mut stored, tree).unwrap();
        }
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        let last_root = stage.questions.remove(12);
        stage.questions.insert(0, last_root);
        stage.preparation_contract_version = 0;
        stored
    }

    #[test]
    fn review_chunk_counts_real_options_caps_questions_and_restores_frozen_boundary() {
        let mut stored = legacy_detail_pool();
        prepare_pool_review(&mut stored, &request("status", 0)).unwrap();
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        let checkpoint = stage.review_checkpoint.as_mut().unwrap();
        checkpoint.unit_budget = 8;
        checkpoint.next_index = 2;
        assert_eq!(
            review_chunk(stage, stage.review_checkpoint.as_ref().unwrap()).len(),
            2
        );
        let option = stage.questions[2].options[0].clone();
        stage.questions[2].options.resize(5, option.clone());
        assert_eq!(
            review_chunk(stage, stage.review_checkpoint.as_ref().unwrap()).len(),
            1
        );
        // Even an oversized legacy question reaches the fail-closed reviewer,
        // rather than producing an empty request or silently dropping it.
        stage.questions[2].options.resize(9, option);
        assert_eq!(
            review_chunk(stage, stage.review_checkpoint.as_ref().unwrap()).len(),
            1
        );
        stage.review_checkpoint.as_mut().unwrap().next_index = 0;
        stage.review_checkpoint.as_mut().unwrap().unit_budget = 96;
        assert_eq!(
            review_chunk(stage, stage.review_checkpoint.as_ref().unwrap()).len(),
            12
        );
        let mut value = serde_json::to_value(&stored).unwrap();
        value["stage_workflow"]["stages"][1]["review_checkpoint"]
            .as_object_mut()
            .unwrap()
            .remove("unit_budget");
        let restored: BrainstormStoredState = serde_json::from_value(value).unwrap();
        let stage = restored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.review_checkpoint.as_ref().unwrap().unit_budget, 8);
        assert_eq!(stage.review_checkpoint.as_ref().unwrap().next_index, 0);
        // A subsequently configured 96-unit limit cannot change an in-flight
        // checkpoint's defaulted or persisted boundary.
        assert_eq!(review_unit_budget(Some("96")), 96);
        assert_eq!(
            review_chunk(stage, stage.review_checkpoint.as_ref().unwrap()).len(),
            1
        );
    }

    #[test]
    fn legacy_pool_review_pass_resumes_chunks_and_preserves_paid_pool_and_ancestor_bindings() {
        let mut stored = legacy_detail_pool();
        let original = serde_json::to_value(&stored).unwrap();
        prepare_pool_review(&mut stored, &request("status", 0)).unwrap();
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .review_checkpoint
            .as_mut()
            .unwrap()
            .unit_budget = 8;
        let first = batch_payload(&request("status", 0), &stored).unwrap();
        assert_eq!(first["kind"], "review");
        assert_eq!(first["review_questions"].as_array().unwrap().len(), 1);
        assert_eq!(first["review_questions"][0]["topic"]["id"], "topic4");
        assert!(!first["review_questions"]
            .to_string()
            .contains("PRIVATE_LONG_SOURCE_BODY"));
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            json!({"review":"pass","codes":[],"decision_ids":[]}),
        )
        .unwrap();
        assert_eq!(
            stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .review_checkpoint
                .as_ref()
                .unwrap()
                .next_index,
            1
        );
        let before_failure = serde_json::to_value(&stored).unwrap();
        for invalid in [
            json!({"review":"reject","codes":["insufficient_context"],"decision_ids":[]}),
            json!({"review":"pass","codes":["topic_scope"],"decision_ids":[]}),
            json!({"review":"reject","codes":["confirmed_decision_reopened"],"decision_ids":["decision_999"]}),
        ] {
            assert!(apply_preparation_step(&mut stored, &request("status", 0), invalid).is_err());
            assert_eq!(serde_json::to_value(&stored).unwrap(), before_failure);
        }
        stored = serde_json::from_value(before_failure).unwrap();
        assert!(!prepare_pool_review(&mut stored, &request("retry_stage", 99)).unwrap());
        let mut reviewed_ids = vec![first["review_questions"][0]["question"]["id"].clone()];
        let mut saw_cross_chunk_ancestor = false;
        while stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .review_checkpoint
            .is_some()
        {
            let payload = batch_payload(&request("status", 0), &stored).unwrap();
            let records = payload["review_questions"].as_array().unwrap();
            assert!((1..=2).contains(&records.len()));
            assert!(
                records
                    .iter()
                    .map(|record| 2 + record["question"]["options"].as_array().unwrap().len())
                    .sum::<usize>()
                    <= 8
            );
            saw_cross_chunk_ancestor |= records.iter().any(|item| {
                item["ancestor_conditions"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|parent| {
                        parent["parent_question_id"]
                            == first["review_questions"][0]["question"]["id"]
                    })
            });
            reviewed_ids.extend(records.iter().map(|item| item["question"]["id"].clone()));
            apply_preparation_step(
                &mut stored,
                &request("status", 0),
                json!({"review":"pass","codes":[],"decision_ids":[]}),
            )
            .unwrap();
            let stage = stored.stage_workflow.as_ref().unwrap().current();
            if let Some(checkpoint) = &stage.review_checkpoint {
                assert_eq!(checkpoint.next_index, reviewed_ids.len());
                assert_eq!(stage.preparation_contract_version, 0);
            }
        }
        assert!(saw_cross_chunk_ancestor);
        assert_eq!(
            reviewed_ids,
            original["stage_workflow"]["stages"][1]["questions"]
                .as_array()
                .unwrap()
                .iter()
                .map(|question| question["id"].clone())
                .collect::<Vec<_>>()
        );
        let mut expected = original;
        expected["stage_workflow"]["stages"][1]["preparation_contract_version"] =
            json!(PREPARATION_CONTRACT_VERSION);
        assert_eq!(serde_json::to_value(&stored).unwrap(), expected);
        assert_eq!(
            batch_payload(&request("status", 0), &stored).unwrap()["kind"],
            "topic_plan"
        );
    }

    #[test]
    fn legacy_pool_review_reject_archives_complete_tree_without_changing_authoritative_state() {
        for code in ["topic_scope", "source_permission_revoked"] {
            let mut stored = legacy_detail_pool();
            begin_topic(&mut stored, &["私有待完成当前主题"]);
            let private_checkpoint = serde_json::to_value(checkpoint(&stored)).unwrap();
            let original = serde_json::to_value(&stored).unwrap();
            prepare_pool_review(&mut stored, &request("status", 0)).unwrap();
            apply_preparation_step(
                &mut stored,
                &request("status", 0),
                json!({"review":"reject","codes":[code],"decision_ids":[]}),
            )
            .unwrap();
            let stage = stored.stage_workflow.as_ref().unwrap().current();
            assert!(stage.questions.is_empty());
            if code == "source_permission_revoked" {
                assert_eq!(stage.progress.completed_batches, 0);
                assert_eq!(stage.progress.total_batches, 0);
                assert_eq!(stage.progress.percent, 0);
                assert!(stage.topics.is_empty());
                let payload = batch_payload(&request("status", 0), &stored).unwrap();
                assert_eq!(payload["kind"], "plan");
                assert!(!payload["plan"].to_string().contains("只落实主题"));
            } else {
                assert_eq!(stage.progress.completed_batches, 1);
                assert_eq!(stage.progress.total_batches, 6);
                assert_eq!(stage.progress.percent, 16);
            }
            assert_eq!(stage.preparation_archives.len(), 1);
            assert_eq!(
                serde_json::to_value(&stage.topic_checkpoint_archives[0].checkpoint).unwrap(),
                private_checkpoint
            );
            assert!(!prepare_pool_review(&mut stored, &request("status", 0)).unwrap());
            let stage = stored.stage_workflow.as_ref().unwrap().current();
            let archive = serde_json::to_value(&stage.preparation_archives[0]).unwrap();
            assert_eq!(
                archive["questions"],
                original["stage_workflow"]["stages"][1]["questions"]
            );
            assert_eq!(
                archive["topics"],
                original["stage_workflow"]["stages"][1]["topics"]
            );
            assert_eq!(
                archive["progress"],
                original["stage_workflow"]["stages"][1]["progress"]
            );
            assert_eq!(archive["reason"], "semantic_review_rejected");
            assert!(archive.get("preparation_archives").is_none());
            let current = serde_json::to_value(&stored).unwrap();
            assert_eq!(current["turns"], original["turns"]);
            assert_eq!(current["root_request"], original["root_request"]);
            assert_eq!(
                current["stage_workflow"]["stages"][0],
                original["stage_workflow"]["stages"][0]
            );
            assert_eq!(
                current["stage_workflow"]["copy_repair_history"],
                original["stage_workflow"]["copy_repair_history"]
            );
            let public =
                serde_json::to_value(brainstorm_response(SESSION, "preparing", 1, &stored))
                    .unwrap();
            assert!(!public.to_string().contains("preparation_archives"));
            assert!(!public.to_string().contains("review_checkpoint"));
            assert!(!public.to_string().contains("PRIVATE_LONG_SOURCE_BODY"));
            let mut edit = request("edit_brief", 0);
            stored.stage_workflow.as_mut().unwrap().current_mut().status = "failed".into();
            edit.brief_edits
                .insert("root_request".into(), "新创作需求".into());
            edit_saved_brief(&mut stored, &edit, 1).unwrap();
            assert_eq!(
                serde_json::to_value(
                    &stored.stage_workflow.as_ref().unwrap().stages[1].preparation_archives[0]
                )
                .unwrap(),
                archive
            );
        }
    }

    #[test]
    fn legacy_review_protects_answered_edited_and_operation_state_and_restarts_changed_input() {
        let pool = legacy_detail_pool();
        for protection in ["answer", "edit", "operation"] {
            let mut stored = pool.clone();
            let question = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
            match protection {
                "answer" => stored.turns.push(BrainstormStoredTurn {
                    question,
                    answer: BrainstormAnswer {
                        selected_option_ids: vec!["a".into()],
                        custom_text: String::new(),
                        source: "user".into(),
                    },
                }),
                "edit" => {
                    stored
                        .brief_edits
                        .insert(question.id, "保留人工修订".into());
                }
                _ => stored
                    .stage_workflow
                    .as_mut()
                    .unwrap()
                    .chapters
                    .push(Chapter {
                        id: "protected".into(),
                        title: "已创作章".into(),
                        goal: "保护正文".into(),
                        status: "writing".into(),
                        question_ids: vec![],
                        instruction_id: None,
                        operation_id: Some("protected-op".into()),
                    }),
            }
            let before = serde_json::to_value(&stored).unwrap();
            assert!(!prepare_pool_review(&mut stored, &request("status", 0)).unwrap());
            assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        }
        let mut changed = pool.clone();
        prepare_pool_review(&mut changed, &request("status", 0)).unwrap();
        apply_pool_review(
            &mut changed,
            &request("status", 0),
            json!({"review":"pass","codes":[],"decision_ids":[]}),
        )
        .unwrap();
        changed
            .brief_edits
            .insert("root_request".into(), "新权限要求".into());
        let before = serde_json::to_value(&changed).unwrap();
        assert!(apply_pool_review(
            &mut changed,
            &request("status", 0),
            json!({"review":"pass","codes":[],"decision_ids":[]})
        )
        .is_err());
        assert_eq!(serde_json::to_value(&changed).unwrap(), before);
        prepare_pool_review(&mut changed, &request("status", 0)).unwrap();
        assert_eq!(
            changed
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .review_checkpoint
                .as_ref()
                .unwrap()
                .next_index,
            0
        );
        let mut unsupported = pool;
        unsupported
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .questions[0]
            .id = "unknown-legacy-id".into();
        prepare_pool_review(&mut unsupported, &request("status", 0)).unwrap();
        assert_eq!(
            unsupported
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .preparation_archives[0]
                .reason,
            "unsupported_legacy_binding"
        );
    }

    fn checkpoint(stored: &BrainstormStoredState) -> &TopicCheckpoint {
        stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .topic_checkpoint
            .as_ref()
            .unwrap()
    }

    fn node_output(stored: &BrainstormStoredState, next: Value) -> Value {
        let cp = checkpoint(stored);
        let goal = cp.frontier.first().unwrap();
        let mut value = question("model_id_must_not_bind", None);
        if cp.preparation_contract_version == PREPARATION_CONTRACT_VERSION
            && cp.atomic_root_questions
        {
            let stage_name = &stored.stage_workflow.as_ref().unwrap().active_stage;
            let binding = cp.root_axes_binding.as_ref().unwrap();
            let axis_id = &cp.root_axis_ids[cp.questions.len()];
            let axis = binding
                .eligible(stage_name)
                .unwrap()
                .into_iter()
                .find(|axis| axis["id"] == *axis_id)
                .unwrap();
            let selection = json!({"options":axis["value_ids"].as_array().unwrap().iter()
                .take(2).enumerate().map(|(index,id)|json!({"value_id":id,
                    "recommended":index==0,"memory_choice":"original"})).collect::<Vec<_>>()});
            let rendered = binding
                .render_values(stage_name, axis_id, &selection, 5)
                .unwrap();
            for field in ["type", "why_now", "single_choice_reason", "answer_template"] {
                value[field] = rendered[field].clone();
            }
            value["prompt"] = rendered["prompt_body"].clone();
            value["dimension_id"] = json!(cp.topic.id);
            value["dimension"] = json!(cp.topic.title);
            value["options"] = json!(rendered["options"]
                .as_array()
                .unwrap()
                .iter()
                .enumerate()
                .map(
                    |(index, option)| json!({"id":((b'a'+index as u8) as char).to_string(),
                    "label":option["label"],"description":option["description"],
                    "recommended":option["recommended"],"details":"已验证来源摘录：保持原文与身份"})
                )
                .collect::<Vec<_>>());
            return json!({"question":value,"next_goals":next,"memory_brief":"本主题已核验来源",
                "root_value_selection":selection});
        }
        let mut trusted: BrainstormQuestion = serde_json::from_value(value.clone()).unwrap();
        trusted.parent_question_id = goal.parent_question_id.clone();
        trusted.parent_option_id = goal.parent_option_id.clone();
        let saved = cp.questions.iter().collect::<Vec<_>>();
        let prefix = prepared_question_prefix(&trusted, &saved).unwrap();
        value["prompt"] = json!(format!("{prefix}{}如何实施？", goal.goal));
        value["single_choice_reason"] = json!("两种组织方式互斥");
        value["parent_question_id"] = json!("model_parent_must_not_bind");
        value["parent_option_id"] = json!("model_option_must_not_bind");
        value["options"][0]["details"] = json!("已验证来源摘录：保持原文与身份");
        json!({"question": value, "next_goals": next, "memory_brief": "本主题已核验来源"})
    }

    fn canonical_root_goals(stage: &str, count: usize) -> Vec<String> {
        root_axes::load()
            .unwrap()
            .eligible(stage)
            .unwrap()
            .iter()
            .take(count)
            .map(|axis| axis["goal"].as_str().unwrap().to_owned())
            .collect()
    }

    fn begin_topic(stored: &mut BrainstormStoredState, goals: &[&str]) {
        let workflow = stored.stage_workflow.as_ref().unwrap();
        let roots = if workflow.current().catalog_binding.is_none()
            && matches!(workflow.active_stage.as_str(), "direction" | "detail")
        {
            canonical_root_goals(&workflow.active_stage, goals.len())
        } else {
            goals.iter().map(|goal| (*goal).to_owned()).collect()
        };
        if matches!(workflow.active_stage.as_str(), "direction" | "detail")
            && workflow.current().catalog_binding.is_none()
        {
            let ids = workflow
                .current()
                .root_axes_binding
                .as_ref()
                .unwrap()
                .validate_goals(&workflow.active_stage, &roots)
                .unwrap();
            let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
            let topic_index = stage.progress.completed_batches - 1;
            stage.topics[topic_index].root_axis_ids = ids;
        }
        apply_preparation_step(stored, &request("status", 0), json!({"root_goals":roots})).unwrap();
    }

    // Preserve genuine version14 tree coverage after registered decisions became
    // atomic in version15. This fixture has the original registry bytes, complete
    // compiled manifest and old input envelope, protected by saved chapter work.
    // It must never authorize tree children through a new atomic checkpoint.
    fn begin_legacy_topic(stored: &mut BrainstormStoredState, goals: &[&str]) {
        begin_topic(stored, goals);
        let old_bytes = include_bytes!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/brainstorm-root-axes-v14.json"
        ));
        let old_binding = root_axes::RootAxesBinding {
            config_sha256: sha256_hex(std::str::from_utf8(old_bytes).unwrap()),
            config: serde_json::from_slice(old_bytes).unwrap(),
        };
        assert_eq!(
            old_binding.config_sha256,
            "cc86de67eaf52d0a42f09374ca284c589c755dec7eecaa6254c21a032c2aa827"
        );
        let workflow = stored.stage_workflow.as_mut().unwrap();
        let stage_name = workflow.active_stage.clone();
        let ids = workflow
            .current()
            .topic_checkpoint
            .as_ref()
            .unwrap()
            .root_axis_ids
            .clone();
        let topic = root_axes::load()
            .unwrap()
            .compile_topics(
                &stage_name,
                &ids,
                1,
                BuildLimits::configured().max_questions_per_topic,
            )
            .unwrap()
            .remove(0);
        let topic: Topic = serde_json::from_value(topic).unwrap();
        let stage = workflow.current_mut();
        stage.preparation_contract_version = 14;
        stage.root_axes_binding = Some(old_binding.clone());
        stage.topics = vec![topic.clone()];
        let cp = stage.topic_checkpoint.as_mut().unwrap();
        cp.preparation_contract_version = 14;
        cp.atomic_root_questions = false;
        cp.root_axes_binding = Some(old_binding);
        cp.topic_id = topic.id.clone();
        cp.topic = topic;
        workflow.chapters = vec![serde_json::from_value(json!({
            "id":"protected-v14-body","title":"保留章节","goal":"既有正文",
            "status":"completed","question_ids":[],"content":"原始已保存正文",
            "operation_id":"original-v14-operation"
        }))
        .unwrap()];
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        let mut old_inputs = json!({
            "session_id":SESSION,"stage":stage_name,"preparation_contract_version":14,
            "root_request":stored.root_request,"decisions":brainstorm_decision_context(stored),
            "brief_edits":stored.brief_edits,"user_input_revisions":stored.user_input_revisions,
            "selected_skills":stored.selected_skills,"limits":stage_limits(),
            "catalog_binding":stage.catalog_binding,"root_axes_binding":stage.root_axes_binding,
            "compiled_topic_manifest":stage.topics
        });
        if let Some(facts) = brainstorm_work_facts(stored) {
            old_inputs["work_facts"] = facts;
        }
        let fingerprint = sha256_hex(&old_inputs.to_string());
        assert_eq!(
            input_fingerprint(&request("status", 0), stored),
            fingerprint
        );
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .input_fingerprint = fingerprint;
    }

    // Build old private tree evidence without replacing the historical published
    // manifest. The caller removes the fixture's temporary chapter protection
    // before testing migrations of otherwise unpaid old contracts.
    fn begin_historical_tree_topic(stored: &mut BrainstormStoredState, goals: &[&str]) {
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        let mut topics = stage.topics.clone();
        let progress = stage.progress.clone();
        let topic = current_topic(stored).unwrap().clone();
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
        stage.root_axes_binding = Some(root_axes::load().unwrap());
        begin_legacy_topic(stored, goals);
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        topics[progress.completed_batches - 1].root_axis_ids = stage
            .topic_checkpoint
            .as_ref()
            .unwrap()
            .root_axis_ids
            .clone();
        stage.topics = topics;
        stage.progress = progress;
        let cp = stage.topic_checkpoint.as_mut().unwrap();
        cp.topic_id = topic.id.clone();
        cp.topic = topic;
        let fingerprint = input_fingerprint(&request("status", 0), stored);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .input_fingerprint = fingerprint;
    }

    fn enable_recovery(stored: &mut BrainstormStoredState, step: usize, stage: usize) {
        let mut recovery = AutomaticRecovery::configured();
        recovery.max_step_retries = step;
        recovery.max_stage_retries = stage;
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .automatic_recovery = Some(recovery);
    }

    #[test]
    fn automatic_recovery_freezes_limits_counts_interrupted_dispatches_and_filters_private_audit() {
        for (value, expected) in [
            (None, 2),
            (Some("0"), 0),
            (Some("5"), 5),
            (Some("6"), 2),
            (Some("+2"), 2),
            (Some(""), 2),
            (Some("1.5"), 2),
        ] {
            assert_eq!(recovery_limit(value, 2, 5), expected);
        }
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["组织方法"]);
        enable_recovery(&mut stored, 2, 8);
        let req = request("status", 0);
        let key = recovery_step_key(&req, &stored).unwrap();
        assert!(reserve_recovery_dispatch(&mut stored, &req).unwrap());
        // Changing generation or request revision cannot buy fresh retry budget.
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .generation_id = uuid::Uuid::new_v4().to_string();
        assert_eq!(
            recovery_step_key(&request("status", 99), &stored).unwrap(),
            key
        );
        let mut restored: BrainstormStoredState =
            serde_json::from_str(&serde_json::to_string(&stored).unwrap()).unwrap();
        assert!(reserve_recovery_dispatch(&mut restored, &req).unwrap());
        assert!(reserve_recovery_dispatch(&mut restored, &req).unwrap());
        let failure = reserve_recovery_dispatch(&mut restored, &req).unwrap_err();
        assert_eq!(failure.1 .0["code"], "BRAINSTORM_PREPARATION_EXHAUSTED");
        let stage = restored.stage_workflow.as_ref().unwrap().current();
        let recovery = stage.automatic_recovery.as_ref().unwrap();
        assert_eq!(
            (recovery.requests_started, recovery.stage_retries_used),
            (3, 2)
        );
        assert_eq!(stage.recovery_history.len(), 3);
        let public =
            serde_json::to_value(restored.stage_workflow.as_ref().unwrap().for_response()).unwrap();
        assert!(public["stages"][0].get("automatic_recovery").is_none());
        assert!(public["stages"][0].get("recovery_history").is_none());
        assert_eq!(public["stages"][0]["recovery_progress"]["attempt"], 2);
        assert!(!public.to_string().contains(&key));
    }

    #[test]
    fn service_outage_waits_without_spending_candidate_retries_and_preserves_history() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["组织方法"]);
        enable_recovery(&mut stored, 2, 8);
        let req = request("status", 0);
        for code in [
            "BRAINSTORM_MODEL_OUTPUT_INVALID",
            "BRAINSTORM_MODEL_OUTPUT_INVALID",
            "BRAINSTORM_MODEL_UNAVAILABLE",
        ] {
            reserve_recovery_dispatch(&mut stored, &req).unwrap();
            let (status, Json(error)) = brainstorm_model_error(code);
            record_preparation_failure(&mut stored, status, error, true);
        }
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.status, "thinking");
        assert!(stage.error.is_none());
        assert_eq!(stage.recovery_history.len(), 3);
        let recovery = stage.automatic_recovery.as_ref().unwrap();
        assert_eq!((recovery.requests_started, recovery.unavailable_failures), (3, 1));
        assert!(recovery.retry_at_ms > now());
        let before = serde_json::to_value(&stage.recovery_history).unwrap();
        let mut restored: BrainstormStoredState =
            serde_json::from_str(&serde_json::to_string(&stored).unwrap()).unwrap();
        reserve_recovery_dispatch(&mut restored, &req).unwrap();
        let stage = restored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(serde_json::to_value(&stage.recovery_history).unwrap(), before);
        let recovery = stage.automatic_recovery.as_ref().unwrap();
        assert_eq!(
            (recovery.requests_started, recovery.stage_retries_used, recovery.unavailable_retries_used),
            (4, 3, 1)
        );
        let (status, Json(error)) = brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID");
        record_preparation_failure(&mut restored, status, error, true);
        let stage = restored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.status, "failed");
        assert_eq!(stage.error.as_ref().unwrap()["code"], "BRAINSTORM_PREPARATION_EXHAUSTED");
        assert_eq!(stage.recovery_history.len(), 4);
        assert_eq!(unavailable_retry_delay(1), 2_000);
        assert_eq!(unavailable_retry_delay(8), 256_000);
        assert_eq!(unavailable_retry_delay(20), 300_000);
    }

    #[test]
    fn old_exhausted_service_failure_resumes_without_resetting_accepted_work() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["组织方法"]);
        enable_recovery(&mut stored, 2, 8);
        let checkpoint_before = serde_json::to_value(checkpoint(&stored)).unwrap();
        let req = request("status", 0);
        let step_key = recovery_step_key(&req, &stored).unwrap();
        let authority_fingerprint = input_fingerprint(&req, &stored);
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        let recovery = stage.automatic_recovery.as_mut().unwrap();
        recovery.step_key = step_key;
        recovery.authority_fingerprint = authority_fingerprint;
        recovery.requests_started = 3;
        recovery.stage_retries_used = 2;
        recovery.last_failure_code = Some("BRAINSTORM_MODEL_UNAVAILABLE".into());
        stage.status = "failed".into();
        stage.error = Some(recovery_exhausted(Some("BRAINSTORM_MODEL_UNAVAILABLE")).1 .0);
        assert!(resume_exhausted_unavailable(&mut stored));
        assert!(!resume_exhausted_unavailable(&mut stored));
        assert_eq!(serde_json::to_value(checkpoint(&stored)).unwrap(), checkpoint_before);
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.status, "thinking");
        assert!(stage.error.is_none());
        let recovery = stage.automatic_recovery.as_ref().unwrap();
        assert_eq!((recovery.requests_started, recovery.stage_retries_used), (3, 2));
        assert_eq!(recovery.unavailable_failures, 1);
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let recovery = stored.stage_workflow.as_ref().unwrap().current().automatic_recovery.as_ref().unwrap();
        assert_eq!((recovery.requests_started, recovery.stage_retries_used), (4, 3));
    }

    #[test]
    fn repeated_outages_keep_a_bounded_audit_and_legacy_counts_migrate_exactly() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["组织方法"]);
        enable_recovery(&mut stored, 2, 1);
        let req = request("status", 0);
        for _ in 0..5 {
            reserve_recovery_dispatch(&mut stored, &req).unwrap();
            let (status, Json(error)) = brainstorm_model_error("BRAINSTORM_MODEL_UNAVAILABLE");
            record_preparation_failure(&mut stored, status, error, true);
        }
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.status, "thinking");
        assert_eq!(stage.recovery_history.len(), 1);
        assert_eq!(stage.recovery_history[0].aggregate_count, 5);
        let recovery = stage.automatic_recovery.as_ref().unwrap();
        assert_eq!(
            (recovery.requests_started, recovery.stage_retries_used,
                recovery.unavailable_failures, recovery.unavailable_retries_used),
            (5, 4, 5, 4)
        );
        let mut legacy = preparing_topic();
        begin_topic(&mut legacy, &["组织方法"]);
        enable_recovery(&mut legacy, 2, 1);
        let step_key = recovery_step_key(&req, &legacy).unwrap();
        let authority_fingerprint = input_fingerprint(&req, &legacy);
        let stage = legacy.stage_workflow.as_mut().unwrap().current_mut();
        stage.status = "failed".into();
        stage.error = Some(recovery_exhausted(Some("BRAINSTORM_MODEL_UNAVAILABLE")).1 .0);
        let recovery = stage.automatic_recovery.as_mut().unwrap();
        recovery.step_key = step_key.clone();
        recovery.authority_fingerprint = authority_fingerprint;
        recovery.requests_started = 3;
        recovery.stage_retries_used = 2;
        recovery.last_failure_code = Some("BRAINSTORM_MODEL_UNAVAILABLE".into());
        for request_number in 1..=3 {
            stage.recovery_history.push(RecoveryAudit {
                step_key: step_key.clone(),
                code: "BRAINSTORM_MODEL_UNAVAILABLE".into(),
                http_status: 503,
                failed_at_ms: request_number as i64,
                requests_started: request_number,
                stage_retries_used: request_number - 1,
                aggregate_count: 0,
            });
        }
        let audit_before = serde_json::to_value(&stage.recovery_history).unwrap();
        assert!(resume_exhausted_unavailable(&mut legacy));
        let stage = legacy.stage_workflow.as_ref().unwrap().current();
        assert_eq!(serde_json::to_value(&stage.recovery_history).unwrap(), audit_before);
        let recovery = stage.automatic_recovery.as_ref().unwrap();
        assert_eq!((recovery.unavailable_failures, recovery.unavailable_retries_used), (3, 2));
        reserve_recovery_dispatch(&mut legacy, &req).unwrap();
        let recovery = legacy.stage_workflow.as_ref().unwrap().current().automatic_recovery.as_ref().unwrap();
        assert_eq!((recovery.requests_started, recovery.stage_retries_used), (4, 3));
    }

    #[test]
    fn legacy_outage_migration_excludes_a_previous_explicit_retry_run() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["组织方法"]);
        enable_recovery(&mut stored, 2, 8);
        let req = request("status", 0);
        let step_key = recovery_step_key(&req, &stored).unwrap();
        let authority_fingerprint = input_fingerprint(&req, &stored);
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.status = "failed".into();
        stage.error = Some(recovery_exhausted(Some("BRAINSTORM_MODEL_UNAVAILABLE")).1 .0);
        let recovery = stage.automatic_recovery.as_mut().unwrap();
        recovery.step_key = step_key.clone();
        recovery.authority_fingerprint = authority_fingerprint;
        recovery.requests_started = 3;
        recovery.stage_retries_used = 2;
        recovery.last_failure_code = Some("BRAINSTORM_MODEL_UNAVAILABLE".into());
        for (code, request_number, stage_retries) in [
            ("MODEL_ACCESS_DENIED", 1, 0),
            ("BRAINSTORM_MODEL_UNAVAILABLE", 1, 0),
            ("BRAINSTORM_MODEL_OUTPUT_INVALID", 2, 1),
            ("BRAINSTORM_MODEL_UNAVAILABLE", 3, 2),
        ] {
            stage.recovery_history.push(RecoveryAudit {
                step_key: step_key.clone(),
                code: code.into(),
                http_status: if code == "BRAINSTORM_MODEL_UNAVAILABLE" { 503 } else { 502 },
                failed_at_ms: request_number as i64,
                requests_started: request_number,
                stage_retries_used: stage_retries,
                aggregate_count: 0,
            });
        }
        let audit_before = serde_json::to_value(&stage.recovery_history).unwrap();
        assert!(resume_exhausted_unavailable(&mut stored));
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(serde_json::to_value(&stage.recovery_history).unwrap(), audit_before);
        let recovery = stage.automatic_recovery.as_ref().unwrap();
        assert_eq!((recovery.unavailable_failures, recovery.unavailable_retries_used), (2, 2));
    }

    #[test]
    fn legacy_outage_migration_counts_an_unrecorded_successful_retry() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["组织方法"]);
        enable_recovery(&mut stored, 2, 1);
        let req = request("status", 0);
        let step_key = recovery_step_key(&req, &stored).unwrap();
        let authority_fingerprint = input_fingerprint(&req, &stored);
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.status = "failed".into();
        stage.error = Some(recovery_exhausted(Some("BRAINSTORM_MODEL_UNAVAILABLE")).1 .0);
        let recovery = stage.automatic_recovery.as_mut().unwrap();
        recovery.step_key = step_key.clone();
        recovery.authority_fingerprint = authority_fingerprint;
        recovery.requests_started = 1;
        recovery.stage_retries_used = 1;
        recovery.last_failure_code = Some("BRAINSTORM_MODEL_UNAVAILABLE".into());
        for (key, stage_retries) in [("previous-step", 0), (step_key.as_str(), 1)] {
            stage.recovery_history.push(RecoveryAudit {
                step_key: key.into(),
                code: "BRAINSTORM_MODEL_UNAVAILABLE".into(),
                http_status: 503,
                failed_at_ms: stage_retries as i64,
                requests_started: 1,
                stage_retries_used: stage_retries,
                aggregate_count: 0,
            });
        }
        assert!(resume_exhausted_unavailable(&mut stored));
        let recovery = stored.stage_workflow.as_ref().unwrap().current().automatic_recovery.as_ref().unwrap();
        assert_eq!((recovery.unavailable_failures, recovery.unavailable_retries_used), (1, 1));
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let recovery = stored.stage_workflow.as_ref().unwrap().current().automatic_recovery.as_ref().unwrap();
        assert_eq!((recovery.stage_retries_used, recovery.unavailable_retries_used), (2, 2));
    }

    #[test]
    fn first_outage_on_new_step_auto_retries_after_stage_quality_budget_is_full() {
        let mut stored = initial();
        enable_recovery(&mut stored, 2, 1);
        let req = request("status", 0);
        let authority_fingerprint = input_fingerprint(&req, &stored);
        let recovery = stored.stage_workflow.as_mut().unwrap().current_mut().automatic_recovery.as_mut().unwrap();
        recovery.authority_fingerprint = authority_fingerprint;
        recovery.stage_retries_used = 1;
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let (status, Json(error)) = brainstorm_model_error("BRAINSTORM_MODEL_UNAVAILABLE");
        record_preparation_failure(&mut stored, status, error, true);
        assert_eq!(stored.stage_workflow.as_ref().unwrap().current().status, "thinking");
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let (status, Json(error)) = brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID");
        record_preparation_failure(&mut stored, status, error, true);
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.status, "failed");
        assert_eq!(stage.error.as_ref().unwrap()["code"], "BRAINSTORM_PREPARATION_EXHAUSTED");
    }

    #[test]
    fn automatic_recovery_keeps_stage_budget_across_steps_and_rejects_terminal_errors() {
        let req = request("status", 0);
        let mut stored = initial();
        enable_recovery(&mut stored, 2, 1);
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let (status, Json(error)) = brainstorm_model_error("BRAINSTORM_MODEL_TIMEOUT");
        record_preparation_failure(&mut stored, status, error, true);
        assert_eq!(
            stored.stage_workflow.as_ref().unwrap().current().status,
            "thinking"
        );
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        apply_batch(&mut stored, plan()).unwrap();
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let recovery = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .automatic_recovery
            .as_ref()
            .unwrap();
        assert_eq!(
            (recovery.requests_started, recovery.stage_retries_used),
            (1, 1)
        );
        let (status, Json(error)) = brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID");
        record_preparation_failure(&mut stored, status, error, true);
        assert_eq!(
            stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .error
                .as_ref()
                .unwrap()["code"],
            "BRAINSTORM_PREPARATION_EXHAUSTED"
        );
        for code in ["MODEL_ACCESS_DENIED", "MODEL_REQUEST_FAILED"] {
            let mut terminal = initial();
            enable_recovery(&mut terminal, 2, 8);
            reserve_recovery_dispatch(&mut terminal, &req).unwrap();
            let (status, Json(error)) = brainstorm_model_error(code);
            record_preparation_failure(&mut terminal, status, error, true);
            let stage = terminal.stage_workflow.as_ref().unwrap().current();
            assert_eq!(stage.status, "failed");
            assert_eq!(stage.error.as_ref().unwrap()["code"], code);
            assert_eq!(
                stage
                    .automatic_recovery
                    .as_ref()
                    .unwrap()
                    .stage_retries_used,
                0
            );
        }
        assert!(!recovery_allowed(
            StatusCode::CONFLICT,
            "BRAINSTORM_MODEL_OUTPUT_INVALID"
        ));
        assert!(!recovery_allowed(
            StatusCode::FORBIDDEN,
            "BRAINSTORM_MODEL_OUTPUT_INVALID"
        ));
        assert!(!recovery_allowed(StatusCode::BAD_GATEWAY, "unknown"));
    }

    #[test]
    fn automatic_recovery_exhaustion_reports_the_actual_failure_without_blame() {
        for (code, message) in [
            (
                "BRAINSTORM_MODEL_OUTPUT_INVALID",
                "本阶段候选未通过内容检查，已保存进度；可继续本阶段思考",
            ),
            (
                "BRAINSTORM_MODEL_TIMEOUT",
                "本阶段思考超时，已保存进度；可继续本阶段思考",
            ),
            (
                "MODEL_RATE_LIMITED",
                "本阶段请求暂时受限，已保存进度；稍后可继续本阶段思考",
            ),
        ] {
            let mut stored = initial();
            enable_recovery(&mut stored, 2, 1);
            let req = request("status", 0);
            for _ in 0..2 {
                reserve_recovery_dispatch(&mut stored, &req).unwrap();
                let (status, Json(error)) = brainstorm_model_error(code);
                record_preparation_failure(&mut stored, status, error, true);
            }
            let stage = stored.stage_workflow.as_ref().unwrap().current();
            assert_eq!(stage.status, "failed");
            let error = stage.error.as_ref().unwrap();
            assert_eq!(error["code"], "BRAINSTORM_PREPARATION_EXHAUSTED");
            assert_eq!(error["retryable"], true);
            assert_eq!(error["message"], message);
            assert_eq!(stage.recovery_history.last().unwrap().code, code);
            let failure = reserve_recovery_dispatch(&mut stored, &req).unwrap_err();
            assert_eq!(failure.1 .0["message"], message);
        }
        assert_eq!(
            recovery_exhausted(Some("BRAINSTORM_MODEL_UNAVAILABLE")).1 .0["message"],
            "本阶段思考服务暂不可用，已保存进度；服务恢复后可继续本阶段思考"
        );
        assert_eq!(
            recovery_exhausted(Some("untrusted upstream message")).1 .0["message"],
            "本阶段暂未准备完成，已保存进度；可继续本阶段思考"
        );
    }

    #[test]
    fn automatic_recovery_new_authority_gets_new_budget_and_preserves_prior_audit() {
        let mut stored = preparing_topic();
        enable_recovery(&mut stored, 2, 1);
        let req = request("status", 0);
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let (status, Json(error)) = brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID");
        record_preparation_failure(&mut stored, status, error, true);
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let previous = serde_json::to_value(
            &stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .recovery_history,
        )
        .unwrap();
        let old_input = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .automatic_recovery
            .as_ref()
            .unwrap()
            .authority_fingerprint
            .clone();
        stored
            .brief_edits
            .insert("root_request".into(), "已确认的新需求".into());
        stored.user_input_revisions.insert("root_request".into(), 7);
        reserve_recovery_dispatch(&mut stored, &req).unwrap();
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        let recovery = stage.automatic_recovery.as_ref().unwrap();
        assert_ne!(recovery.authority_fingerprint, old_input);
        assert_eq!(
            (recovery.requests_started, recovery.stage_retries_used),
            (1, 0)
        );
        assert_eq!(
            serde_json::to_value(&stage.recovery_history).unwrap(),
            previous
        );
    }

    #[test]
    fn generator_contract_v7_upgrade_archives_entire_unanswered_manifest_checkpoint_and_chapters() {
        assert_generator_contract_upgrade_preserves_history(7);
    }

    #[test]
    fn generator_contract_v8_upgrade_preserves_previous_archives_and_recovery_audit() {
        assert_generator_contract_upgrade_preserves_history(8);
    }

    #[test]
    fn generator_contract_v9_upgrade_preserves_previous_archives_and_recovery_audit() {
        assert_generator_contract_upgrade_preserves_history(9);
    }

    #[test]
    fn generator_contract_v10_upgrade_archives_unconfirmed_factual_authority_and_preserves_history()
    {
        assert_generator_contract_upgrade_preserves_history(10);
    }

    #[test]
    fn generator_contract_v11_upgrade_archives_unconfirmed_skill_policy_violations_and_preserves_history(
    ) {
        assert_generator_contract_upgrade_preserves_history(11);
    }

    #[test]
    fn generator_contract_v13_upgrade_preserves_full_history() {
        assert_generator_contract_upgrade_preserves_history(13);
    }

    #[test]
    fn generator_contract_v12_upgrade_preserves_full_history() {
        assert_generator_contract_upgrade_preserves_history(12);
    }

    #[test]
    fn generator_contract_v10_failed_direction_replans_manifest_instead_of_reusing_old_topics() {
        let mut stored = initial();
        let topics = (0..6)
            .map(|index| {
                json!({"id":format!("old-{index}"),"title":format!("旧主题{index}"),
                    "goal":format!("历史规划目标{index}")})
            })
            .collect::<Vec<_>>();
        apply_batch(&mut stored, json!({"topics":topics,"chapters":[]})).unwrap();
        begin_historical_tree_topic(&mut stored, &["未经确认的受众前提", "历史周报周期"]);
        for index in 0..2 {
            let output = node_output(
                &stored,
                json!([
                    {"option_id":"a","goals":[format!("历史周期执行甲{index}")]},
                    {"option_id":"b","goals":[format!("历史周期执行乙{index}")]}]),
            );
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        }
        stored.stage_workflow.as_mut().unwrap().chapters.clear();
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = 10;
        stage.root_axes_binding = Some(root_axes::load().unwrap());
        let cp = stage.topic_checkpoint.as_mut().unwrap();
        cp.frontier[0].goal = "未经确认的受众前提".into();
        cp.frontier[1].goal = "历史周报周期".into();
        cp.root_axis_ids.clear();
        cp.root_axes_binding = None;
        stage
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .preparation_contract_version = 10;
        stage.status = "failed".into();
        stage.error = Some(
            recovery_exhausted(Some("BRAINSTORM_MODEL_OUTPUT_INVALID"))
                .1
                 .0,
        );
        let before = serde_json::to_value(&stored).unwrap();
        assert!(refresh_generator_contract(&mut stored).unwrap());
        let after = serde_json::to_value(&stored).unwrap();
        for field in [
            "root_request",
            "selected_skills",
            "turns",
            "brief_edits",
            "user_input_revisions",
            "archived_questions",
            "memory_brief",
        ] {
            assert_eq!(after[field], before[field]);
        }
        for index in 1..3 {
            assert_eq!(
                after["stage_workflow"]["stages"][index],
                before["stage_workflow"]["stages"][index]
            );
        }
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.preparation_archives.len(), 1);
        assert_eq!(stage.topic_checkpoint_archives.len(), 1);
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[0].checkpoint).unwrap(),
            before["stage_workflow"]["stages"][0]["topic_checkpoint"]
        );
        assert_eq!(
            serde_json::to_value(&stage.preparation_archives[0].topics).unwrap(),
            before["stage_workflow"]["stages"][0]["topics"]
        );
        assert!(stage.questions.is_empty());
        assert!(stage.topics.is_empty());
        assert!(stage.topic_checkpoint.is_none());
        assert_eq!(stage.progress.completed_batches, 0);
        assert_eq!(stage.progress.total_batches, 0);
        assert_eq!(
            batch_payload(&request("status", 0), &stored).unwrap()["kind"],
            "plan"
        );
        assert!(!refresh_generator_contract(&mut stored).unwrap());
    }

    fn assert_generator_contract_upgrade_preserves_history(old_version: u32) {
        let mut stored = ready();
        stored.turns.push(BrainstormStoredTurn {
            question: stored.current_question.take().unwrap(),
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                source: "user".into(),
                ..Default::default()
            },
        });
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.stages[0].status = "completed".into();
        workflow.stages[0].preparation_contract_version = 0;
        workflow.stages[1].status = "completed".into();
        workflow.stages[1].preparation_contract_version = 7;
        workflow.active_stage = "writing".into();
        workflow.current_mut().status = "thinking".into();
        apply_batch(&mut stored, json!({"topics":[{"id":"chapter-topic","title":"章节一","goal":"旧坏目标","chapter_id":"one"}],
            "chapters":[{"id":"one","title":"章节一","goal":"章节范围"}]})).unwrap();
        begin_topic(&mut stored, &["旧根目标"]);
        let output = node_output(
            &stored,
            json!([{"option_id":"a","goals":["旧甲目标"]},{"option_id":"b","goals":["旧乙目标"]}]),
        );
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = old_version;
        stage
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .preparation_contract_version = old_version;
        stage.status = "failed".into();
        stage.error = Some(
            brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID")
                .1
                 .0,
        );
        if old_version >= 8 {
            stage.status = "thinking".into();
            stage.error = None;
        }
        let old_checkpoint = stage.topic_checkpoint.as_ref().unwrap().clone();
        stage
            .topic_checkpoint_archives
            .push(TopicCheckpointArchive {
                reason: "contract_changed".into(),
                archived_at_ms: 17,
                checkpoint: old_checkpoint,
            });
        stage.preparation_archives.push(PreparationArchive {
            reason: "generator_contract_changed".into(),
            archived_at_ms: 17,
            preparation_contract_version: 7,
            generation_id: "previous-v7-generation".into(),
            status: "failed".into(),
            questions: stage.questions.clone(),
            topics: stage.topics.clone(),
            progress: stage.progress.clone(),
            codes: vec![],
            decision_ids: vec![],
            catalog_binding: None,
            root_axes_binding: None,
            chapters: vec![],
            error: stage.error.clone(),
            automatic_recovery: None,
        });
        let mut recovery = AutomaticRecovery::configured();
        recovery.requests_started = 3;
        recovery.stage_retries_used = 2;
        recovery.step_key = "previous-step-hash".into();
        recovery.request_in_flight = true;
        recovery.authority_fingerprint = "previous-authority-hash".into();
        recovery.retry_at_ms = 29;
        recovery.last_failure_code = Some("BRAINSTORM_MODEL_TIMEOUT".into());
        stage.recovery_history.push(RecoveryAudit {
            step_key: recovery.step_key.clone(),
            code: "BRAINSTORM_MODEL_TIMEOUT".into(),
            http_status: 504,
            failed_at_ms: 17,
            requests_started: recovery.requests_started,
            stage_retries_used: recovery.stage_retries_used,
            aggregate_count: 0,
        });
        stage.automatic_recovery = Some(recovery);
        let before = serde_json::to_value(&stored).unwrap();
        assert!(refresh_generator_contract(&mut stored).unwrap());
        let after = serde_json::to_value(&stored).unwrap();
        for field in [
            "turns",
            "root_request",
            "selected_skills",
            "brief_edits",
            "user_input_revisions",
        ] {
            assert_eq!(after[field], before[field]);
        }
        assert_eq!(
            after["stage_workflow"]["stages"][0],
            before["stage_workflow"]["stages"][0]
        );
        assert_eq!(
            after["stage_workflow"]["stages"][1],
            before["stage_workflow"]["stages"][1]
        );
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(
            stage.preparation_contract_version,
            PREPARATION_CONTRACT_VERSION
        );
        assert_eq!(stage.status, "thinking");
        assert!(stage.topics.is_empty());
        assert!(stage.topic_checkpoint.is_none());
        assert!(stage.automatic_recovery.is_none());
        assert!(stored.stage_workflow.as_ref().unwrap().chapters.is_empty());
        assert_eq!(stage.topic_checkpoint_archives.len(), 2);
        assert_eq!(stage.preparation_archives.len(), 2);
        for field in ["topic_checkpoint_archives", "preparation_archives"] {
            assert_eq!(
                after["stage_workflow"]["stages"][2][field][0],
                before["stage_workflow"]["stages"][2][field][0]
            );
        }
        assert_eq!(
            after["stage_workflow"]["stages"][2]["recovery_history"],
            before["stage_workflow"]["stages"][2]["recovery_history"]
        );
        assert_eq!(
            stage.topic_checkpoint_archives[1].reason,
            "contract_changed"
        );
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[1].checkpoint).unwrap(),
            before["stage_workflow"]["stages"][2]["topic_checkpoint"]
        );
        let archive = serde_json::to_value(&stage.preparation_archives[1]).unwrap();
        assert_eq!(archive["reason"], "generator_contract_changed");
        for field in [
            "topics",
            "questions",
            "progress",
            "error",
            "preparation_contract_version",
            "automatic_recovery",
        ] {
            assert_eq!(archive[field], before["stage_workflow"]["stages"][2][field]);
        }
        assert_eq!(archive["chapters"], before["stage_workflow"]["chapters"]);
        assert_eq!(archive["automatic_recovery"]["request_in_flight"], true);
        let public =
            serde_json::to_string(&stored.stage_workflow.as_ref().unwrap().for_response()).unwrap();
        assert!(!public.contains("automatic_recovery"));
        assert!(!public.contains("previous-step-hash"));
        let restored: BrainstormStoredState = serde_json::from_value(after.clone()).unwrap();
        assert_eq!(serde_json::to_value(restored).unwrap(), after);
        assert!(!refresh_generator_contract(&mut stored).unwrap());
        for kind in ["operation", "answer", "brief_edit", "copy_repair"] {
            let mut protected: BrainstormStoredState =
                serde_json::from_value(before.clone()).unwrap();
            if kind == "operation" {
                protected.stage_workflow.as_mut().unwrap().chapters[0].operation_id =
                    Some("protected-operation".into());
            } else {
                let question = checkpoint(&protected).questions[0].clone();
                protected
                    .stage_workflow
                    .as_mut()
                    .unwrap()
                    .current_mut()
                    .questions
                    .push(question.clone());
                match kind {
                    "answer" => protected.turns.push(BrainstormStoredTurn {
                        question,
                        answer: BrainstormAnswer {
                            custom_text: "保留确认".into(),
                            source: "user".into(),
                            ..Default::default()
                        },
                    }),
                    "brief_edit" => {
                        protected
                            .brief_edits
                            .insert(question.id, "保留人工修订".into());
                    }
                    _ => protected
                        .stage_workflow
                        .as_mut()
                        .unwrap()
                        .copy_repair_history
                        .push(CandidateCopyRepair {
                            revision: 5,
                            question_id: question.id,
                            option_id: None,
                            field: CandidateCopyField::WhyNow,
                            old_text: "原提问说明".into(),
                            new_text: "用户修订说明".into(),
                        }),
                }
            }
            let original = serde_json::to_value(&protected).unwrap();
            assert!(!refresh_generator_contract(&mut protected).unwrap());
            assert_eq!(serde_json::to_value(protected).unwrap(), original);
        }
    }

    #[tokio::test]
    async fn automatic_recovery_restart_resumes_only_failed_node_and_never_publishes_rejected_candidate(
    ) {
        use axum::response::IntoResponse;
        let mut stored = preparing_topic();
        // Three program-owned atomic properties retain the same paid-prefix
        // recovery behavior without inventing child decisions.
        begin_topic(&mut stored, &["阅读对象", "表达目的", "表达侧重"]);
        let root = node_output(&stored, json!([]));
        apply_preparation_step(&mut stored, &request("status", 0), root).unwrap();
        enable_recovery(&mut stored, 2, 8);
        let paid_root = serde_json::to_value(&checkpoint(&stored).questions[0]).unwrap();
        let first = node_output(&stored, json!([]));
        let mut accepted = stored.clone();
        apply_preparation_step(&mut accepted, &request("status", 0), first.clone()).unwrap();
        let second = node_output(&accepted, json!([]));
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move |Json(body): Json<Value>| {
                let count = observed.clone();
                let first = first.clone();
                let second = second.clone();
                async move {
                    assert_eq!(body["kind"], "node");
                    match count.fetch_add(1, Ordering::SeqCst) {
                        0 => {
                            let mut invalid = first;
                            invalid["question"]["prompt"] = json!("");
                            invalid["memory_brief"] = json!("不得保存的失败材料");
                            Json(invalid).into_response()
                        }
                        1 => Json(first).into_response(),
                        _ => Json(second).into_response(),
                    }
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let (tmp, state) = fixture(Some(&stored), &url);
        act(&state, &request("status", 0)).await.unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                let saved = decode(&load(&state)).unwrap();
                if !saved
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .current()
                    .recovery_history
                    .is_empty()
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let waiting = decode(&load(&state)).unwrap();
        assert_eq!(
            waiting.stage_workflow.as_ref().unwrap().current().status,
            "thinking"
        );
        assert!(waiting
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .error
            .is_none());
        assert_eq!(
            serde_json::to_value(&checkpoint(&waiting).questions[0]).unwrap(),
            paid_root
        );
        assert_eq!(checkpoint(&waiting).questions.len(), 1);
        assert!(!waiting.memory_brief.contains("不得保存"));
        let public = act(&state, &request("status", 0)).await.unwrap();
        assert_eq!(
            public
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .recovery_progress
                .as_ref()
                .unwrap()
                .attempt,
            1
        );
        for job in state.brainstorm_stage_jobs.lock().unwrap().values() {
            job.abort();
        }
        let restarted = AppState::with_service_urls(
            StorageManager::open(&tmp.path().join("stages.db")).unwrap(),
            url.clone(),
            url.clone(),
            vec![],
        );
        let durable_requests = decode(&load(&restarted))
            .unwrap()
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .automatic_recovery
            .as_ref()
            .unwrap()
            .requests_started;
        assert_eq!(durable_requests, 1);
        act(&restarted, &request("status", 0)).await.unwrap();
        wait_stage(&restarted, "answering").await;
        let complete = decode(&load(&restarted)).unwrap();
        let stage = complete.stage_workflow.as_ref().unwrap().current();
        assert_eq!(
            serde_json::to_value(&stage.questions[0]).unwrap(),
            paid_root
        );
        assert_eq!(stage.questions.len(), 3);
        assert_eq!(count.load(Ordering::SeqCst), 3);
        assert_eq!(stage.recovery_history.len(), 1);
        assert_eq!(
            stage
                .automatic_recovery
                .as_ref()
                .unwrap()
                .stage_retries_used,
            1
        );
        assert!(complete.turns.is_empty());
        assert!(!serde_json::to_string(&complete)
            .unwrap()
            .contains("不得保存"));
        server.abort();
    }

    #[tokio::test]
    async fn automatic_recovery_preflight_invalid_checkpoint_is_terminal_without_model_calls() {
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move || {
                let count = observed.clone();
                async move {
                    count.fetch_add(1, Ordering::SeqCst);
                    Json(plan())
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        for invalid_kind in ["empty_frontier", "invalid_budget", "broken_parent"] {
            let mut stored = preparing_topic();
            begin_topic(&mut stored, &["实施方法"]);
            enable_recovery(&mut stored, 2, 8);
            let cp = stored
                .stage_workflow
                .as_mut()
                .unwrap()
                .current_mut()
                .topic_checkpoint
                .as_mut()
                .unwrap();
            match invalid_kind {
                "empty_frontier" => cp.frontier.clear(),
                "invalid_budget" => cp.frontier[0].depth = 99,
                _ => {
                    cp.frontier[0].depth = 2;
                    cp.frontier[0].parent_question_id = Some("missing-parent".into());
                    cp.frontier[0].parent_option_id = Some("a".into());
                }
            }
            let original = serde_json::to_value(checkpoint(&stored)).unwrap();
            let (_tmp, state) = fixture(Some(&stored), &url);
            act(&state, &request("status", 0)).await.unwrap();
            wait_stage(&state, "failed").await;
            let saved = decode(&load(&state)).unwrap();
            assert_eq!(
                saved
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .current()
                    .error
                    .as_ref()
                    .unwrap()["code"],
                if invalid_kind == "empty_frontier" {
                    "BRAINSTORM_PREPARATION_INPUT_INVALID"
                } else {
                    "BRAINSTORM_STAGE_CONFLICT"
                }
            );
            assert_eq!(serde_json::to_value(checkpoint(&saved)).unwrap(), original);
            assert_eq!(count.load(Ordering::SeqCst), 0);
            let revision = load(&state).revision;
            act(&state, &request("status", 0)).await.unwrap();
            assert_eq!(load(&state).revision, revision);
        }
        server.abort();
    }

    #[tokio::test]
    async fn automatic_recovery_never_retries_http_authority_input_or_cancel_boundaries() {
        for (status, expected) in [
            (401, "MODEL_ACCESS_DENIED"),
            (403, "MODEL_ACCESS_DENIED"),
            (409, "BRAINSTORM_STAGE_CONFLICT"),
            (422, "MODEL_REQUEST_FAILED"),
            (499, "BRAINSTORM_STAGE_CONFLICT"),
        ] {
            let mut stored = preparing_topic();
            enable_recovery(&mut stored, 2, 8);
            let count = Arc::new(AtomicUsize::new(0));
            let observed = count.clone();
            let router = axum::Router::new().route("/creation/brainstorm/stage/batch", axum::routing::post(move || {
                let count = observed.clone(); async move { count.fetch_add(1, Ordering::SeqCst);
                    // Even an upstream misclassification cannot change the actual
                    // authorization/input/cancellation HTTP boundary into a retry.
                    (StatusCode::from_u16(status).unwrap(), Json(json!({"detail":{"code":"BRAINSTORM_MODEL_OUTPUT_INVALID","retryable":true}}))) }
            }));
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async move {
                axum::serve(listener, router).await.unwrap();
            });
            let (_tmp, state) = fixture(Some(&stored), &url);
            act(&state, &request("status", 0)).await.unwrap();
            wait_stage(&state, "failed").await;
            let failed = decode(&load(&state)).unwrap();
            let stage = failed.stage_workflow.as_ref().unwrap().current();
            assert_eq!(stage.error.as_ref().unwrap()["code"], expected);
            assert_eq!(stage.error.as_ref().unwrap()["retryable"], false);
            assert_eq!(
                stage
                    .automatic_recovery
                    .as_ref()
                    .unwrap()
                    .stage_retries_used,
                0
            );
            assert_eq!(count.load(Ordering::SeqCst), 1);
            act(&state, &request("status", 0)).await.unwrap();
            assert_eq!(count.load(Ordering::SeqCst), 1);
            server.abort();
        }
    }

    #[tokio::test]
    async fn automatic_recovery_abandon_during_wait_never_dispatches_another_request() {
        let mut stored = preparing_topic();
        enable_recovery(&mut stored, 2, 8);
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move || {
                let count = observed.clone();
                async move {
                    count.fetch_add(1, Ordering::SeqCst);
                    (
                        StatusCode::BAD_GATEWAY,
                        Json(json!({"detail":{"code":"BRAINSTORM_MODEL_OUTPUT_INVALID"}})),
                    )
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let (_tmp, state) = fixture(Some(&stored), &url);
        act(&state, &request("status", 0)).await.unwrap();
        tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if !decode(&load(&state))
                    .unwrap()
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .current()
                    .recovery_history
                    .is_empty()
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let revision = load(&state).revision;
        let stopped = act(&state, &request("abandon", revision)).await.unwrap();
        assert_eq!(stopped.phase, "abandoned");
        tokio::time::sleep(Duration::from_millis(1_150)).await;
        assert_eq!(count.load(Ordering::SeqCst), 1);
        assert_eq!(load(&state).phase, "abandoned");
        assert_eq!(
            decode(&load(&state))
                .unwrap()
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .recovery_history
                .len(),
            1
        );
        server.abort();
    }

    #[tokio::test]
    async fn automatic_recovery_exhaustion_is_terminal_and_status_does_not_restart() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["实施方法"]);
        enable_recovery(&mut stored, 1, 8);
        let checkpoint_before = serde_json::to_value(checkpoint(&stored)).unwrap();
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move || {
                let count = observed.clone();
                async move {
                    count.fetch_add(1, Ordering::SeqCst);
                    (
                        StatusCode::BAD_GATEWAY,
                        Json(json!({"detail":{"code":"BRAINSTORM_MODEL_OUTPUT_INVALID"}})),
                    )
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let (_tmp, state) = fixture(Some(&stored), &url);
        act(&state, &request("status", 0)).await.unwrap();
        wait_stage(&state, "failed").await;
        let failed = decode(&load(&state)).unwrap();
        let stage = failed.stage_workflow.as_ref().unwrap().current();
        assert_eq!(
            stage.error.as_ref().unwrap()["code"],
            "BRAINSTORM_PREPARATION_EXHAUSTED"
        );
        assert_eq!(
            serde_json::to_value(checkpoint(&failed)).unwrap(),
            checkpoint_before
        );
        assert_eq!(stage.recovery_history.len(), 2);
        assert_eq!(count.load(Ordering::SeqCst), 2);
        let revision = load(&state).revision;
        act(&state, &request("status", 0)).await.unwrap();
        assert_eq!(load(&state).revision, revision);
        assert_eq!(count.load(Ordering::SeqCst), 2);
        server.abort();
    }

    #[test]
    fn topic_checkpoints_keep_multiple_roots_and_independent_child_goals_until_atomic_publish() {
        let mut stored = preparing_topic();
        begin_legacy_topic(&mut stored, &["组织路线", "资源路线"]);
        let mut ids = vec![];
        for next in [
            json!([{"option_id":"b","goals":["分布组织验证"]},
                {"option_id":"a","goals":["集中组织执行","集中组织指标"]}]),
            json!([{"option_id":"a","goals":["资源执行"]},{"option_id":"b","goals":["资源验证"]}]),
            json!([]),
            json!([]),
            json!([]),
            json!([]),
            json!([]),
        ] {
            ids.push(checkpoint(&stored).frontier[0].id.clone());
            let output = node_output(&stored, next);
            let phase = apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
            let stage = stored.stage_workflow.as_ref().unwrap().current();
            if phase == "preparing" {
                assert_eq!(stage.progress.completed_batches, 1);
                assert!(stage.questions.is_empty());
                assert!(stored.current_question.is_none());
                assert!(stored.memory_brief.is_empty());
            }
        }
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(
            stage
                .questions
                .iter()
                .map(|question| question.id.clone())
                .collect::<Vec<_>>(),
            ids
        );
        assert_eq!(stage.questions.len(), 7);
        assert_eq!(stage.progress.completed_batches, 2);
        assert_eq!(stage.status, "answering");
        assert!(stage.topic_checkpoint.is_none());
        assert!(stage.questions[0].parent_question_id.is_none());
        assert!(stage.questions[1].parent_question_id.is_none());
        assert_eq!(stage.questions[2].parent_option_id.as_deref(), Some("b"));
        assert_eq!(stage.questions[3].parent_option_id.as_deref(), Some("a"));
        assert_eq!(stage.questions[4].parent_option_id.as_deref(), Some("a"));
        assert!(stage
            .questions
            .iter()
            .all(|question| question.options[0].details == "已验证来源摘录：保持原文与身份"));
        assert_eq!(stored.memory_brief, "本主题已核验来源");
    }

    #[test]
    fn node_budget_reserves_complete_descendants_and_three_level_frontier_is_breadth_first() {
        let mut stored = preparing_topic();
        begin_legacy_topic(&mut stored, &["完整路线"]);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .limits = BuildLimits {
            max_questions_per_topic: 7,
            max_depth: 3,
        };
        let req = request("status", 0);
        let payload = batch_payload(&req, &stored).unwrap();
        assert_eq!(payload["node"]["max_options"], 2);
        assert_eq!(payload["node"]["max_next_goals"], 2);
        for depth in [1, 2, 2, 3, 3, 3, 3] {
            assert_eq!(checkpoint(&stored).frontier[0].depth, depth);
            let next = if depth < 3 {
                json!([{"option_id":"a","goals":[format!("节点{}甲执行", checkpoint(&stored).questions.len())]},
                    {"option_id":"b","goals":[format!("节点{}乙验证", checkpoint(&stored).questions.len())]}])
            } else {
                json!([])
            };
            if depth == 3 {
                let payload = batch_payload(&req, &stored).unwrap();
                assert_eq!(
                    payload["node"]["ancestor_conditions"]
                        .as_array()
                        .unwrap()
                        .len(),
                    2
                );
                assert_eq!(payload["node"]["max_next_goals"], 0);
            }
            let output = node_output(&stored, next);
            apply_preparation_step(&mut stored, &req, output).unwrap();
        }
        assert_eq!(
            stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .questions
                .len(),
            7
        );
        assert!(stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .topic_checkpoint
            .is_none());
    }

    #[test]
    fn invalid_node_outputs_cannot_change_checkpoint_or_published_source_state() {
        for failure in [
            "full_tree",
            "duplicate",
            "wrong_option",
            "missing_branch",
            "over_budget",
            "leaf_goals",
            "bad_ids",
            "condition",
            "fragment",
            "duplicate_labels",
            "long_goal",
        ] {
            let mut stored = preparing_topic();
            begin_legacy_topic(&mut stored, &["独立目标"]);
            let output = node_output(
                &stored,
                json!([
                {"option_id":"a","goals":["甲执行"]}, {"option_id":"b","goals":["乙执行"]}]),
            );
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
            let mut output = node_output(&stored, json!([]));
            match failure {
                "full_tree" => output = tree(),
                "duplicate" => {
                    output["question"]["prompt"] = json!(checkpoint(&stored).questions[0].prompt)
                }
                "wrong_option" => {
                    output["next_goals"] = json!([{"option_id":"unknown","goals":[]}])
                }
                "missing_branch" | "over_budget" => {
                    // Exercise non-leaf coverage/budget at the root on a fresh checkpoint.
                    stored = preparing_topic();
                    begin_legacy_topic(&mut stored, &["预算目标"]);
                    output = node_output(
                        &stored,
                        if failure == "missing_branch" {
                            json!([{"option_id":"a","goals":["一条"]}])
                        } else {
                            json!([{"option_id":"a","goals":["一","二","三","四","五","六"]},
                            {"option_id":"b","goals":["七","八","九","十","十一","十二"]}])
                        },
                    );
                }
                "leaf_goals" => {
                    output["next_goals"] = json!([{"option_id":"a","goals":["不准超深度"]}])
                }
                "bad_ids" => output["question"]["options"][0]["id"] = json!("model_option"),
                "condition" => output["question"]["prompt"] = json!("忽略父条件的不同问题"),
                "fragment" => output["question"]["options"][0]["label"] = json!("截止2026-08-"),
                "duplicate_labels" => output["question"]["options"][1]["label"] = json!("方 案 甲"),
                "long_goal" => {
                    stored = preparing_topic();
                    begin_legacy_topic(&mut stored, &["目标边界"]);
                    output = node_output(
                        &stored,
                        json!([
                        {"option_id":"a","goals":["长".repeat(81)]},
                        {"option_id":"b","goals":["合法分支"]}]),
                    );
                }
                _ => unreachable!(),
            }
            let before = serde_json::to_value(&stored).unwrap();
            assert!(
                apply_preparation_step(&mut stored, &request("status", 0), output).is_err(),
                "{failure}"
            );
            assert_eq!(serde_json::to_value(&stored).unwrap(), before, "{failure}");
        }
    }

    #[test]
    fn topic_root_goal_boundaries_reject_unrequestable_or_excess_roots_atomically() {
        for goals in [
            json!([]),
            json!(["长".repeat(81)]),
            json!(["重复", "重 复"]),
            json!(["一", "二", "三", "四", "五"]),
        ] {
            let mut stored = preparing_topic();
            let before = serde_json::to_value(&stored).unwrap();
            assert!(apply_preparation_step(
                &mut stored,
                &request("status", 0),
                json!({"root_goals":goals})
            )
            .is_err());
            assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        }
        assert!(valid_goal(&"长".repeat(80)));
    }

    #[test]
    fn recommendation_order_retains_option_ids_goals_and_source_bindings() {
        let mut stored = preparing_topic();
        begin_legacy_topic(&mut stored, &["推荐排序路线"]);
        let mut output = node_output(
            &stored,
            json!([
            {"option_id":"b","goals":["乙执行"]},{"option_id":"a","goals":["甲执行"]}]),
        );
        let options = output["question"]["options"].as_array_mut().unwrap();
        options[0]["recommended"] = json!(false);
        options[1]["recommended"] = json!(true);
        options.swap(0, 1);
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        assert_eq!(checkpoint(&stored).questions[0].options[0].id, "b");
        assert_eq!(
            checkpoint(&stored).frontier[0].parent_option_id.as_deref(),
            Some("b")
        );
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        assert_eq!(
            payload["node"]["ancestor_conditions"][0]["option_label"],
            "方案乙"
        );
        assert_eq!(
            checkpoint(&stored).questions[0].options[1].details,
            "已验证来源摘录：保持原文与身份"
        );
        for _ in 0..2 {
            let output = node_output(&stored, json!([]));
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        }
        let questions = &stored.stage_workflow.as_ref().unwrap().current().questions;
        assert_eq!(questions[0].options[0].id, "b");
        assert_eq!(questions[1].parent_option_id.as_deref(), Some("b"));
        assert_eq!(questions[2].parent_option_id.as_deref(), Some("a"));
    }

    #[test]
    fn work_fact_changes_alter_checkpoint_identity_but_style_and_absent_context_do_not() {
        let mut stored = preparing_topic();
        let req = request("status", 0);
        let original = input_fingerprint(&req, &stored);
        stored.work_context = Some(
            serde_json::json!({"profile_id":"proposal_review", "style_samples":["表达样例"], "review_enabled":true}),
        );
        assert_eq!(input_fingerprint(&req, &stored), original);
        stored.work_context.as_mut().unwrap()["purpose"] = json!("决定是否批准试点");
        let with_facts = input_fingerprint(&req, &stored);
        assert_ne!(with_facts, original);
        stored.work_context.as_mut().unwrap()["style_samples"] = json!(["另一个风格示例"]);
        stored.work_context.as_mut().unwrap()["profile_id"] = json!("project_report");
        assert_eq!(input_fingerprint(&req, &stored), with_facts);
        assert!(!brainstorm_brief(&stored).contains("风格示例"));
    }

    #[test]
    fn checkpoint_identity_uses_authoritative_inputs_and_response_hides_private_nodes() {
        let mut stored = preparing_topic();
        begin_legacy_topic(&mut stored, &["私有未完成目标"]);
        let output = node_output(
            &stored,
            json!([
            {"option_id":"a","goals":["私有甲目标"]},{"option_id":"b","goals":["私有乙目标"]}]),
        );
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        let original = input_fingerprint(&request("status", 0), &stored);
        let private = serde_json::to_string(&stored).unwrap();
        assert!(private.contains("topic_checkpoint"));
        assert!(private.contains(&canonical_root_goals("direction", 1)[0]));
        let public =
            serde_json::to_value(brainstorm_response(SESSION, "preparing", 5, &stored)).unwrap();
        let stage = &public["stage_workflow"]["stages"][0];
        assert!(stage.get("topic_checkpoint").is_none());
        assert_eq!(stage["preparation_progress"]["saved_nodes"], 1);
        assert_eq!(stage["preparation_progress"]["pending_nodes"], 2);
        // Canonical compiled topic goals are public planning metadata; private
        // node state and the unpaid future goals must remain hidden.
        let mut public_topics = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .topics
            .clone();
        for topic in &mut public_topics {
            topic.root_axis_ids.clear();
        }
        assert_eq!(
            stage["topics"],
            serde_json::to_value(public_topics).unwrap()
        );
        assert!(stage["topics"][0].get("root_axis_ids").is_none());
        assert!(!public.to_string().contains("私有甲目标"));
        assert!(!public.to_string().contains("私有乙目标"));
        assert!(stage.get("topic_checkpoint_archives").is_none());
        stored.memory_brief = "派生记忆发生增长，不是用户输入".into();
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .generation_id = "retry_worker".into();
        assert_eq!(input_fingerprint(&request("status", 99), &stored), original);
        for field in ["root", "skills", "revision", "permission"] {
            let mut changed = stored.clone();
            match field {
                "root" => changed.root_request.push_str("新增要求"),
                "skills" => changed.selected_skills.push(json!({"id":"new-skill"})),
                "revision" => {
                    changed
                        .user_input_revisions
                        .insert("root_request".into(), 9);
                }
                "permission" => {
                    changed
                        .brief_edits
                        .insert("root_request".into(), "禁止检索我的历史记忆".into());
                    changed
                        .user_input_revisions
                        .insert("root_request".into(), 10);
                }
                _ => unreachable!(),
            }
            assert_ne!(input_fingerprint(&request("status", 0), &changed), original);
            let before_rejection = serde_json::to_value(&changed).unwrap();
            assert!(apply_preparation_step(
                &mut changed,
                &request("status", 0),
                node_output(&stored, json!([])),
            )
            .is_err());
            assert_eq!(serde_json::to_value(&changed).unwrap(), before_rejection);
            refresh_checkpoint_input(&mut changed, &request("status", 0));
            assert!(changed
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .topic_checkpoint
                .is_none());
            assert!(changed
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .questions
                .is_empty());
            let stage = changed.stage_workflow.as_ref().unwrap().current();
            assert_eq!(stage.topic_checkpoint_archives.len(), 1);
            assert_eq!(stage.topic_checkpoint_archives[0].reason, "input_changed");
            assert_eq!(
                serde_json::to_value(&stage.topic_checkpoint_archives[0].checkpoint).unwrap(),
                serde_json::to_value(checkpoint(&stored)).unwrap()
            );
            refresh_checkpoint_input(&mut changed, &request("status", 0));
            assert_eq!(
                changed
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .current()
                    .topic_checkpoint_archives
                    .len(),
                1
            );
        }
    }

    #[tokio::test]
    async fn old_contract_checkpoint_is_archived_persisted_and_replanned_without_published_changes()
    {
        let mut stored = initial();
        apply_batch(
            &mut stored,
            json!({"topics":[
            {"id":"old","title":"已发布主题","goal":"原有效决定"},
            {"id":"remaining","title":"剩余主题","goal":"只落实当前主题目标"}]}),
        )
        .unwrap();
        apply_batch(&mut stored, four_question_tree()).unwrap();
        let published_root = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
        stored.turns.push(BrainstormStoredTurn {
            question: published_root.clone(),
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: "保留用户原话".into(),
                source: "user".into(),
            },
        });
        stored.selected_skills = vec![json!({"id":"saved-skill"})];
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .copy_repair_history
            .push(CandidateCopyRepair {
                revision: 9,
                question_id: published_root.id,
                option_id: None,
                field: CandidateCopyField::WhyNow,
                old_text: "原候选说明".into(),
                new_text: "已审计候选说明".into(),
            });
        begin_historical_tree_topic(
            &mut stored,
            &[
                "失效私有根甲",
                "失效私有根乙",
                "失效私有根丙",
                "失效私有根丁",
            ],
        );
        for index in 0..4 {
            let output = node_output(
                &stored,
                json!([
                {"option_id":"a","goals":[format!("失效私有子甲{index}")]},
                {"option_id":"b","goals":[format!("失效私有子乙{index}")]}]),
            );
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        }
        stored.stage_workflow.as_mut().unwrap().chapters.clear();
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.status = "failed".into();
        stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
        stage.root_axes_binding = Some(root_axes::load().unwrap());
        let mut legacy = serde_json::to_value(&stored).unwrap();
        legacy["stage_workflow"]["stages"][0]["topic_checkpoint"]
            .as_object_mut()
            .unwrap()
            .remove("preparation_contract_version");
        let mut stored: BrainstormStoredState = serde_json::from_value(legacy).unwrap();
        let old_checkpoint = serde_json::to_value(checkpoint(&stored)).unwrap();
        assert_eq!(old_checkpoint["preparation_contract_version"], 0);
        assert_eq!(old_checkpoint["questions"].as_array().unwrap().len(), 4);
        let mut before = serde_json::to_value(&stored).unwrap();
        before["stage_workflow"]["stages"][0]
            .as_object_mut()
            .unwrap()
            .remove("topic_checkpoint");
        refresh_checkpoint_input(&mut stored, &request("retry_stage", 1));
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert!(stage.topic_checkpoint.is_none());
        let archive = &stage.topic_checkpoint_archives[0];
        assert_eq!(archive.reason, "contract_changed");
        assert!(archive.archived_at_ms > 0);
        assert_eq!(
            serde_json::to_value(&archive.checkpoint).unwrap(),
            old_checkpoint
        );
        let mut after = serde_json::to_value(&stored).unwrap();
        after["stage_workflow"]["stages"][0]
            .as_object_mut()
            .unwrap()
            .remove("topic_checkpoint_archives");
        assert_eq!(after, before);
        assert_eq!(
            batch_payload(&request("status", 0), &stored).unwrap()["kind"],
            "topic_plan"
        );
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let reloaded = decode(&load(&state)).unwrap();
        assert_eq!(
            serde_json::to_value(&reloaded).unwrap(),
            serde_json::to_value(&stored).unwrap()
        );
        let response =
            serde_json::to_value(act(&state, &request("status", 0)).await.unwrap()).unwrap();
        assert!(response["stage_workflow"]["stages"][0]
            .get("topic_checkpoint_archives")
            .is_none());
        assert!(!response.to_string().contains("失效私有"));
        let current_binding = root_axes::load().unwrap();
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
        stage.root_axes_binding = Some(current_binding);
        begin_topic(&mut stored, &["新契约当前主题"]);
        assert_eq!(
            checkpoint(&stored).preparation_contract_version,
            PREPARATION_CONTRACT_VERSION
        );
        assert_ne!(
            checkpoint(&stored).build_id,
            old_checkpoint["build_id"].as_str().unwrap()
        );
        assert_eq!(
            stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .topic_checkpoint_archives
                .len(),
            1
        );
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        assert_eq!(
            payload["plan"]["prepared_question_bodies"]
                .as_array()
                .unwrap()
                .len(),
            4
        );
        assert!(!payload["plan"].to_string().contains("失效私有"));
    }

    #[test]
    fn prepared_bodies_use_only_exact_saved_ancestor_prefixes_and_preserve_legacy_prompts() {
        let mut root: BrainstormQuestion = serde_json::from_value(question("root", None)).unwrap();
        root.options[0].label = "路径（A）".into();
        let mut child: BrainstormQuestion =
            serde_json::from_value(question("child", Some(("root", "a")))).unwrap();
        child.options[0].label = "方案「甲」".into();
        child.prompt = "若「路径（A）」，比较中间方法？".into();
        let mut leaf: BrainstormQuestion =
            serde_json::from_value(question("leaf", Some(("child", "a")))).unwrap();
        leaf.prompt = "若「路径（A）」且「方案「甲」」，比较最终验证方法？".into();
        let saved = [&root, &child];
        assert_eq!(prepared_question_body(&root, &saved), root.prompt);
        assert_eq!(prepared_question_body(&child, &saved), "比较中间方法？");
        assert_eq!(prepared_question_body(&leaf, &saved), "比较最终验证方法？");
        leaf.prompt = "若「路径A」且「方案甲」，比较最终验证方法？".into();
        assert_eq!(prepared_question_body(&leaf, &saved), leaf.prompt);
        leaf.prompt = "既有条件式题干全文保持".into();
        assert_eq!(prepared_question_body(&leaf, &saved), leaf.prompt);
        leaf.parent_question_id = Some("missing".into());
        assert_eq!(prepared_question_body(&leaf, &saved), leaf.prompt);
    }

    #[test]
    fn conditional_nodes_reject_same_body_without_mutation_and_accept_distinct_body() {
        let mut stored = preparing_topic();
        begin_legacy_topic(&mut stored, &["比较主要方法"]);
        let output = node_output(
            &stored,
            json!([
            {"option_id":"a","goals":["甲执行设计"]},{"option_id":"b","goals":["乙执行设计"]}]),
        );
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        let mut first = node_output(&stored, json!([]));
        first["question"]["prompt"] = json!("若「方案甲」，共同执行决定？");
        apply_preparation_step(&mut stored, &request("status", 0), first).unwrap();
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        assert_eq!(
            payload["plan"]["prepared_question_bodies"][1],
            "共同执行决定？"
        );
        let mut duplicate = node_output(&stored, json!([]));
        duplicate["question"]["prompt"] = json!("若「方案乙」，共同执行决定？");
        let before = serde_json::to_value(&stored).unwrap();
        assert!(
            apply_preparation_step(&mut stored, &request("status", 0), duplicate.clone()).is_err()
        );
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        duplicate["question"]["prompt"] = json!("若「方案乙」，另一个执行决定？");
        apply_preparation_step(&mut stored, &request("status", 0), duplicate).unwrap();
        assert!(stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .topic_checkpoint
            .is_none());
        assert_eq!(
            stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .questions
                .len(),
            3
        );
    }

    #[test]
    fn node_gate_requires_complete_ordered_literal_ancestor_prefix_and_nonempty_body() {
        let mut stored = preparing_topic();
        begin_legacy_topic(&mut stored, &["比较根路线"]);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .limits = BuildLimits {
            max_questions_per_topic: 7,
            max_depth: 3,
        };
        for (index, next) in [
            json!([{"option_id":"a","goals":["甲路径"]},{"option_id":"b","goals":["乙路径"]}]),
            json!([{"option_id":"a","goals":["深层甲"]},{"option_id":"b","goals":["深层乙"]}]),
            json!([{"option_id":"a","goals":["深层丙"]},{"option_id":"b","goals":["深层丁"]}]),
        ]
        .into_iter()
        .enumerate()
        {
            let mut output = node_output(&stored, next);
            if index == 1 {
                output["question"]["options"][0]["label"] = json!("执行 路径甲（GPU）");
            }
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        }
        let valid = node_output(&stored, json!([]));
        assert!(valid["question"]["prompt"]
            .as_str()
            .unwrap()
            .starts_with("若「方案甲」且「执行 路径甲（GPU）」，"));
        for invalid_prompt in [
            "若「执行 路径甲（GPU）」，深层甲如何实施？",
            "若「执行 路径甲（GPU）」且「方案甲」，深层甲如何实施？",
            "若「方案甲」且「执行路径甲GPU」，深层甲如何实施？",
            "补充：若「方案甲」且「执行 路径甲（GPU）」，深层甲如何实施？",
            "若「方案甲」且「执行 路径甲（GPU）」，",
        ] {
            let before = serde_json::to_value(&stored).unwrap();
            let mut invalid_node = valid.clone();
            invalid_node["question"]["prompt"] = json!(invalid_prompt);
            assert!(
                apply_preparation_step(&mut stored, &request("status", 0), invalid_node).is_err()
            );
            assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        }
        apply_preparation_step(&mut stored, &request("status", 0), valid).unwrap();
        assert_eq!(checkpoint(&stored).questions.len(), 4);
    }

    #[test]
    fn root_replan_retains_checkpoint_archives_and_archives_current_private_candidates() {
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["第一次私有目标"]);
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .preparation_contract_version = 0;
        refresh_checkpoint_input(&mut stored, &request("status", 0));
        let first = serde_json::to_value(
            &stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .topic_checkpoint_archives[0],
        )
        .unwrap();
        begin_legacy_topic(&mut stored, &["新版私有目标"]);
        let output = node_output(
            &stored,
            json!([{"option_id":"a","goals":["私有甲"]},{"option_id":"b","goals":["私有乙"]}]),
        );
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        let second = serde_json::to_value(checkpoint(&stored)).unwrap();
        // The fixture's body only selected the protected old-tree path while
        // creating saved candidates. This edit test represents unpaid work.
        stored.stage_workflow.as_mut().unwrap().chapters.clear();
        stored.stage_workflow.as_mut().unwrap().current_mut().status = "failed".into();
        let mut edit = request("edit_brief", 0);
        edit.brief_edits
            .insert("root_request".into(), "新创作需求".into());
        edit_saved_brief(&mut stored, &edit, 1).unwrap();
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert!(stage.topic_checkpoint.is_none());
        assert_eq!(stage.topic_checkpoint_archives.len(), 2);
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[0]).unwrap(),
            first
        );
        assert_eq!(stage.topic_checkpoint_archives[1].reason, "input_changed");
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[1].checkpoint).unwrap(),
            second
        );
        assert!(
            !serde_json::to_string(&stored.stage_workflow.as_ref().unwrap().for_response())
                .unwrap()
                .contains("私有")
        );
    }

    #[test]
    fn writing_checkpoint_binds_chapter_only_after_complete_topic_and_retains_copy_audit() {
        let mut stored = initial();
        stored.stage_workflow.as_mut().unwrap().active_stage = "writing".into();
        apply_batch(
            &mut stored,
            json!({"topics":[{"id":"t","title":"开篇","goal":"开篇设计","chapter_id":"one"}],
            "chapters":[{"id":"one","title":"开篇","goal":"开篇设计"}]}),
        )
        .unwrap();
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .copy_repair_history
            .push(CandidateCopyRepair {
                revision: 7,
                question_id: "archived-q".into(),
                option_id: None,
                field: CandidateCopyField::WhyNow,
                old_text: "旧说明".into(),
                new_text: "候选说明".into(),
            });
        let audit =
            serde_json::to_value(&stored.stage_workflow.as_ref().unwrap().copy_repair_history)
                .unwrap();
        begin_topic(&mut stored, &["开篇目标"]);
        for next in [
            json!([{"option_id":"a","goals":["开篇甲"]},{"option_id":"b","goals":["开篇乙"]}]),
            json!([]),
            json!([]),
        ] {
            let output = node_output(&stored, next);
            let phase = apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
            let w = stored.stage_workflow.as_ref().unwrap();
            assert_eq!(serde_json::to_value(&w.copy_repair_history).unwrap(), audit);
            if phase == "preparing" {
                assert!(w.chapters[0].question_ids.is_empty());
                assert!(w.active_chapter_id.is_none());
            }
        }
        let w = stored.stage_workflow.as_ref().unwrap();
        assert_eq!(w.active_chapter_id.as_deref(), Some("one"));
        assert_eq!(
            w.chapters[0].question_ids,
            w.current()
                .questions
                .iter()
                .map(|q| q.id.clone())
                .collect::<Vec<_>>()
        );
        assert_eq!(w.chapters[0].status, "answering");
    }

    fn http_node_output(body: &Value) -> Value {
        let node = &body["node"];
        if body["node_mode"]["kind"] == "root_axis_atomic" {
            let binding: RootAxesBinding =
                serde_json::from_value(body["root_axes_binding"].clone()).unwrap();
            let stage = body["stage"].as_str().unwrap();
            let axis_id = body["node_mode"]["axis_id"].as_str().unwrap();
            let axis = binding
                .eligible(stage)
                .unwrap()
                .into_iter()
                .find(|axis| axis["id"] == axis_id)
                .unwrap();
            let selection = json!({"options":axis["value_ids"].as_array().unwrap().iter()
                .take(2).enumerate().map(|(index,id)|json!({"value_id":id,
                    "recommended":index==0,"memory_choice":"original"})).collect::<Vec<_>>()});
            let rendered = binding
                .render_values(stage, axis_id, &selection, 5)
                .unwrap();
            let mut q = question("untrusted_model_id", None);
            for field in ["type", "why_now", "single_choice_reason", "answer_template"] {
                q[field] = rendered[field].clone();
            }
            q["prompt"] = rendered["prompt_body"].clone();
            q["dimension_id"] = body["topic"]["id"].clone();
            q["dimension"] = body["topic"]["title"].clone();
            q["options"] = json!(rendered["options"]
                .as_array()
                .unwrap()
                .iter()
                .enumerate()
                .map(
                    |(index, option)| json!({"id":((b'a'+index as u8) as char).to_string(),
                    "label":option["label"],"description":option["description"],
                    "recommended":option["recommended"],"details":"已核验来源字段"})
                )
                .collect::<Vec<_>>());
            return json!({"question":q,"next_goals":[],"memory_brief":"本主题来源",
                "root_value_selection":selection});
        }
        let conditions = node["ancestor_conditions"].as_array().unwrap();
        let prefix = if conditions.is_empty() {
            String::new()
        } else {
            format!(
                "若{}，",
                conditions
                    .iter()
                    .map(|parent| format!("「{}」", parent["option_label"].as_str().unwrap()))
                    .collect::<Vec<_>>()
                    .join("且")
            )
        };
        let mut q = question("untrusted_model_id", None);
        q["prompt"] = json!(format!(
            "{prefix}{}如何实施？",
            node["goal"].as_str().unwrap()
        ));
        q["single_choice_reason"] = json!("两种组织方式互斥");
        q["options"][0]["details"] = json!("已核验来源字段");
        let next = if body["node_mode"]["kind"] != "root_axis_atomic"
            && node["depth"].as_u64().unwrap() < body["limits"]["max_depth"].as_u64().unwrap()
        {
            // Each independent root owns different descendant decisions.
            let ordinal = node["id"].as_str().unwrap().rsplit('-').next().unwrap();
            json!([{"option_id":"a","goals":[format!("根{ordinal}甲执行")]},{"option_id":"b","goals":[format!("根{ordinal}乙执行")]}])
        } else {
            json!([])
        };
        json!({"question":q,"next_goals":next,"memory_brief":"本主题来源"})
    }

    fn http_preparation_output(body: &Value) -> Value {
        match body["kind"].as_str().unwrap() {
            "plan" => compiled_plan_for_request(body),
            "topic_plan" => compiled_topic_roots(body),
            "node" => http_node_output(body),
            "content_questions" => json!({
                "questions":[{
                    "id":"deliverable", "dimension_id":"deliverable",
                    "dimension":"团队方案交付", "type":"multi_choice",
                    "prompt":"团队方案的本次交付应先确定哪一项？",
                    "why_now":"明确本次团队方案的交付重点。",
                    "required":true, "allow_custom":true,
                    "options":[
                        {"id":"a","label":"执行计划","description":"正文列出执行计划和责任分工。","recommended":true},
                        {"id":"b","label":"决策建议","description":"正文列出待决策事项和判断依据。","recommended":false}
                    ]
                }],
                "content_proofs":[{
                    "question_id":"deliverable", "anchor_source":"root_request",
                    "anchor_quote":"团队方案", "document_effect":"确定正文的主要交付对象。",
                    "option_effects":["正文展开执行步骤和责任分工。","正文展开决策事项和判断依据。"]
                }]
            }),
            other => panic!("unexpected preparation step {other}"),
        }
    }

    #[tokio::test]
    async fn unanswered_legacy_ready_pools_review_before_direct_answer_without_confirmation() {
        for (status, action) in [
            ("answering", "status"),
            ("ready", "status"),
            ("answering", "answer"),
        ] {
            let mut stored = legacy_detail_pool();
            let question = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
            stored.stage_workflow.as_mut().unwrap().current_mut().status = status.into();
            stored.current_question = Some(question.clone());
            let before_questions =
                serde_json::to_value(&stored.stage_workflow.as_ref().unwrap().current().questions)
                    .unwrap();
            let before_turns = serde_json::to_value(&stored.turns).unwrap();
            let seen = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
            let calls = seen.clone();
            let router = axum::Router::new().route(
                "/creation/brainstorm/stage/batch",
                axum::routing::post(move |Json(body): Json<Value>| {
                    let calls = calls.clone();
                    async move {
                        calls.lock().unwrap().push(body);
                        std::future::pending::<Json<Value>>().await
                    }
                }),
            );
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
            let (_tmp, state) = fixture(Some(&stored), &url);
            let mut req = request(action, 0);
            if action == "answer" {
                req.question_id = Some(question.id);
                req.answer = Some(BrainstormAnswer {
                    selected_option_ids: vec!["a".into()],
                    custom_text: "尚未确认的提交".into(),
                    source: "user".into(),
                });
            }
            assert_eq!(act(&state, &req).await.is_err(), action == "answer");
            tokio::time::timeout(Duration::from_secs(2), async {
                while seen.lock().unwrap().is_empty() {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            let session = load(&state);
            assert_eq!(session.phase, "preparing");
            let current = decode(&session).unwrap();
            let stage = current.stage_workflow.as_ref().unwrap().current();
            assert_eq!(stage.status, "thinking");
            assert!(stage.review_checkpoint.is_some());
            assert!(stage.preparation_archives.is_empty());
            assert!(current.current_question.is_none());
            assert_eq!(
                serde_json::to_value(&stage.questions).unwrap(),
                before_questions
            );
            assert_eq!(serde_json::to_value(&current.turns).unwrap(), before_turns);
            assert_eq!(seen.lock().unwrap()[0]["kind"], "review");
            server.abort();
        }
    }

    #[tokio::test]
    async fn protected_legacy_status_remains_readable_without_stamping_or_mutating_history() {
        for protection in ["answer", "edit", "operation"] {
            let mut stored = legacy_detail_pool();
            stored.stage_workflow.as_mut().unwrap().current_mut().status = "answering".into();
            let question = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
            stored.current_question = Some(question.clone());
            match protection {
                "answer" => stored.turns.push(BrainstormStoredTurn {
                    question,
                    answer: BrainstormAnswer {
                        selected_option_ids: vec!["a".into()],
                        custom_text: "既有确认".into(),
                        source: "user".into(),
                    },
                }),
                "edit" => {
                    stored
                        .brief_edits
                        .insert(question.id, "既有人工修订".into());
                }
                _ => stored
                    .stage_workflow
                    .as_mut()
                    .unwrap()
                    .chapters
                    .push(Chapter {
                        id: "protected".into(),
                        title: "既有正文".into(),
                        goal: "保持正文".into(),
                        status: "writing".into(),
                        question_ids: vec![],
                        instruction_id: None,
                        operation_id: Some("existing-operation".into()),
                    }),
            }
            let original = serde_json::to_value(&stored).unwrap();
            let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
            let response = act(&state, &request("status", 0)).await.unwrap();
            assert_eq!(response.revision, 0);
            assert_eq!(
                serde_json::to_value(decode(&load(&state)).unwrap()).unwrap(),
                original
            );
            assert_eq!(
                response
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .current()
                    .preparation_contract_version,
                0
            );
            assert!(state.brainstorm_stage_jobs.lock().unwrap().is_empty());
        }
    }

    #[tokio::test]
    async fn legacy_review_network_failure_restart_retry_resume_reviewed_prefix_without_repayment()
    {
        let requests = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
        let seen = requests.clone();
        let fail_once = Arc::new(std::sync::atomic::AtomicBool::new(true));
        let failure = fail_once.clone();
        let router = axum::Router::new().route("/creation/brainstorm/stage/batch", axum::routing::post(move |Json(body): Json<Value>| {
            let seen = seen.clone(); let failure = failure.clone();
            async move {
                let call_index = { let mut calls = seen.lock().unwrap(); calls.push(body.clone()); calls.len() };
                if body["kind"] == "topic_plan" { std::future::pending::<()>().await; }
                if call_index == 2 && failure.swap(false, std::sync::atomic::Ordering::SeqCst) {
                    return (StatusCode::GATEWAY_TIMEOUT, Json(json!({"detail":{"code":"BRAINSTORM_MODEL_TIMEOUT","retryable":true}}))).into_response();
                }
                Json(json!({"review":"pass","codes":[],"decision_ids":[]})).into_response()
            }
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let mut stored = legacy_detail_pool();
        let original = serde_json::to_value(&stored).unwrap();
        prepare_pool_review(&mut stored, &request("status", 0)).unwrap();
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .review_checkpoint
            .as_mut()
            .unwrap()
            .unit_budget = 8;
        let (tmp, state) = fixture(Some(&stored), &url);
        act(&state, &request("status", 0)).await.unwrap();
        wait_stage(&state, "failed").await;
        let failed_session = load(&state);
        let failed = decode(&failed_session).unwrap();
        let stage = failed.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.review_checkpoint.as_ref().unwrap().next_index, 1);
        assert_eq!(stage.review_checkpoint.as_ref().unwrap().unit_budget, 8);
        assert!(stage.preparation_archives.is_empty());
        assert_eq!(
            serde_json::to_value(&stage.questions).unwrap(),
            original["stage_workflow"]["stages"][1]["questions"]
        );
        let restarted = Arc::new(AppState::with_service_urls(
            StorageManager::open(&tmp.path().join("stages.db")).unwrap(),
            url.clone(),
            url,
            vec![],
        ));
        act(&restarted, &request("retry_stage", failed_session.revision))
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(4), async {
            while requests
                .lock()
                .unwrap()
                .last()
                .is_none_or(|body| body["kind"] != "topic_plan")
            {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let resumed = decode(&load(&restarted)).unwrap();
        let stage = resumed.stage_workflow.as_ref().unwrap().current();
        assert_eq!(
            stage.preparation_contract_version,
            PREPARATION_CONTRACT_VERSION
        );
        assert!(stage.review_checkpoint.is_none());
        assert!(stage.preparation_archives.is_empty());
        assert_eq!(
            serde_json::to_value(&stage.questions).unwrap(),
            original["stage_workflow"]["stages"][1]["questions"]
        );
        assert_eq!(
            serde_json::to_value(&resumed.turns).unwrap(),
            original["turns"]
        );
        let calls = requests.lock().unwrap();
        assert_eq!(calls[0]["review_questions"].as_array().unwrap().len(), 1);
        assert_eq!(calls[1]["review_questions"], calls[2]["review_questions"]);
        assert_eq!(calls[1]["context_id"], calls[2]["context_id"]);
        let accepted_ids = calls
            .iter()
            .enumerate()
            .filter(|(index, body)| *index != 1 && body["kind"] == "review")
            .flat_map(|(_, body)| {
                body["review_questions"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|record| record["question"]["id"].clone())
            })
            .collect::<Vec<_>>();
        assert_eq!(
            accepted_ids,
            original["stage_workflow"]["stages"][1]["questions"]
                .as_array()
                .unwrap()
                .iter()
                .map(|question| question["id"].clone())
                .collect::<Vec<_>>()
        );
        for body in calls.iter().filter(|body| body["kind"] == "review") {
            let records = body["review_questions"].as_array().unwrap();
            assert!(
                records
                    .iter()
                    .map(|record| 2 + record["question"]["options"].as_array().unwrap().len())
                    .sum::<usize>()
                    <= 8
            );
        }
        assert_eq!(calls.last().unwrap()["kind"], "topic_plan");
        assert_eq!(calls.last().unwrap()["topic"]["id"], "topic5");
        server.abort();
    }

    #[tokio::test]
    async fn thinking_v3_review_stamp_is_rechecked_and_zero_node_checkpoint_archived_with_history()
    {
        let mut stored = legacy_detail_pool();
        for version in [0, 2] {
            begin_historical_tree_topic(&mut stored, &["旧契约私有根目标"]);
            let output = node_output(
                &stored,
                json!([{"option_id":"a","goals":["私有甲执行"]},{"option_id":"b","goals":["私有乙执行"]}]),
            );
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
            stored
                .stage_workflow
                .as_mut()
                .unwrap()
                .current_mut()
                .topic_checkpoint
                .as_mut()
                .unwrap()
                .preparation_contract_version = version;
            assert!(refresh_checkpoint_input(&mut stored, &request("status", 0)));
        }
        let prior_archives = serde_json::to_value(
            &stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .topic_checkpoint_archives,
        )
        .unwrap();
        begin_historical_tree_topic(
            &mut stored,
            &["待准备甲", "待准备乙", "待准备丙", "待准备丁"],
        );
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.chapters.clear();
        let stage = workflow.current_mut();
        // A previous checker stamped the pool; a contract upgrade must recheck it.
        stage.preparation_contract_version = 3;
        stage.root_axes_binding = Some(root_axes::load().unwrap());
        stage
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .preparation_contract_version = 3;
        workflow.copy_repair_history.push(CandidateCopyRepair {
            revision: 9,
            question_id: workflow.current().questions[0].id.clone(),
            option_id: None,
            field: CandidateCopyField::WhyNow,
            old_text: "原候选说明".into(),
            new_text: "修订后的候选说明".into(),
        });
        let original = serde_json::to_value(&stored).unwrap();
        let old_checkpoint = serde_json::to_value(checkpoint(&stored)).unwrap();
        assert!(old_checkpoint["questions"].as_array().unwrap().is_empty());
        assert_eq!(old_checkpoint["frontier"].as_array().unwrap().len(), 4);
        let old_generation = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .generation_id
            .clone();
        let requests = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
        let seen = requests.clone();
        let release = Arc::new(tokio::sync::Notify::new());
        let resumed = release.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move |Json(body): Json<Value>| {
                let seen = seen.clone();
                let resumed = resumed.clone();
                async move {
                    seen.lock().unwrap().push(body.clone());
                    if body["kind"] == "review" {
                        resumed.notified().await;
                        Json(json!({"review":"reject","codes":["topic_scope"],"decision_ids":[]}))
                    } else {
                        std::future::pending::<Json<Value>>().await
                    }
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let (_tmp, state) = fixture(Some(&stored), &url);
        let old_worker_key = format!("{SESSION}:{old_generation}");
        state
            .brainstorm_stage_jobs
            .lock()
            .unwrap()
            .insert(old_worker_key.clone(), tokio::spawn(std::future::pending()));
        let response = act(&state, &request("status", 0)).await.unwrap();
        assert_eq!(response.revision, 1);
        let public = serde_json::to_value(response).unwrap().to_string();
        for private_field in [
            "topic_checkpoint",
            "review_checkpoint",
            "preparation_archives",
            "旧契约私有",
            "待准备甲",
        ] {
            assert!(!public.contains(private_field));
        }
        assert!(!state
            .brainstorm_stage_jobs
            .lock()
            .unwrap()
            .contains_key(&old_worker_key));
        tokio::time::timeout(Duration::from_secs(2), async {
            while requests.lock().unwrap().is_empty() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let reviewing = decode(&load(&state)).unwrap();
        let stage = reviewing.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.preparation_contract_version, 3);
        assert_ne!(stage.generation_id, old_generation);
        assert!(stage.topic_checkpoint.is_none());
        assert_eq!(stage.topic_checkpoint_archives.len(), 3);
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[..2]).unwrap(),
            prior_archives
        );
        assert_eq!(
            stage.topic_checkpoint_archives[2].reason,
            "contract_changed"
        );
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[2].checkpoint).unwrap(),
            old_checkpoint
        );
        let review = stage.review_checkpoint.as_ref().unwrap();
        assert_eq!(
            review.preparation_contract_version,
            PREPARATION_CONTRACT_VERSION
        );
        assert_eq!(review.next_index, 0);
        assert_eq!(
            serde_json::to_value(&stage.questions).unwrap(),
            original["stage_workflow"]["stages"][1]["questions"]
        );
        assert_eq!(requests.lock().unwrap()[0]["kind"], "review");
        assert_eq!(
            requests.lock().unwrap()[0]["review_questions"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert!(persist(&state, SESSION, 0, "preparing", &stored).is_err());
        release.notify_one();
        tokio::time::timeout(Duration::from_secs(2), async {
            while requests.lock().unwrap().len() < 2 {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let rebuilt = decode(&load(&state)).unwrap();
        let workflow = rebuilt.stage_workflow.as_ref().unwrap();
        let stage = workflow.current();
        assert_eq!(
            stage.preparation_contract_version,
            PREPARATION_CONTRACT_VERSION
        );
        assert!(stage.questions.is_empty());
        assert!(stage.review_checkpoint.is_none());
        assert_eq!(stage.progress.completed_batches, 1);
        assert_eq!(stage.preparation_archives.len(), 1);
        let archive = &stage.preparation_archives[0];
        assert_eq!(archive.reason, "semantic_review_rejected");
        assert_eq!(archive.preparation_contract_version, 3);
        assert_eq!(archive.generation_id, old_generation);
        assert_eq!(
            serde_json::to_value(&archive.questions).unwrap(),
            original["stage_workflow"]["stages"][1]["questions"]
        );
        assert_eq!(
            serde_json::to_value(&stage.topics).unwrap(),
            original["stage_workflow"]["stages"][1]["topics"]
        );
        assert_eq!(
            serde_json::to_value(&archive.progress).unwrap(),
            original["stage_workflow"]["stages"][1]["progress"]
        );
        assert_eq!(
            serde_json::to_value(&workflow.stages[0]).unwrap(),
            original["stage_workflow"]["stages"][0]
        );
        assert_eq!(
            serde_json::to_value(&rebuilt.turns).unwrap(),
            original["turns"]
        );
        assert_eq!(
            serde_json::to_value(&workflow.copy_repair_history).unwrap(),
            original["stage_workflow"]["copy_repair_history"]
        );
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives).unwrap(),
            serde_json::to_value(
                &reviewing
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .current()
                    .topic_checkpoint_archives
            )
            .unwrap()
        );
        let calls = requests.lock().unwrap();
        assert_eq!(calls[1]["kind"], "topic_plan");
        assert_eq!(calls[1]["topic"]["id"], "topic1");
        drop(calls);
        let public = serde_json::to_string(&workflow.for_response()).unwrap();
        assert!(!public.contains("preparation_archives"));
        assert!(!public.contains("topic_checkpoint"));
        assert!(!public.contains("review_checkpoint"));
        server.abort();
    }

    #[tokio::test]
    async fn thinking_empty_legacy_pool_archives_nodes_keeps_prior_versions_and_replans_topic() {
        for (invalid_version, saved_nodes, pending_nodes) in [(4, 4, 8), (5, 2, 6)] {
            let mut stored = legacy_detail_pool();
            archive_preparation_pool(
                &mut stored,
                "semantic_review_rejected",
                vec!["topic_scope".into()],
                vec![],
            )
            .unwrap();
            let workflow = stored.stage_workflow.as_mut().unwrap();
            let stage = workflow.current_mut();
            stage.preparation_contract_version = invalid_version;
            workflow.copy_repair_history.push(CandidateCopyRepair {
                revision: 9,
                question_id: "archived-candidate".into(),
                option_id: None,
                field: CandidateCopyField::WhyNow,
                old_text: "原候选说明".into(),
                new_text: "修订后的候选说明".into(),
            });
            let prior_versions: Vec<_> = [0, 2, 3, 4]
                .into_iter()
                .filter(|version| version < &invalid_version)
                .collect();
            let prior_count = prior_versions.len();
            for version in prior_versions {
                if version == 4 {
                    begin_historical_tree_topic(
                        &mut stored,
                        &[
                            "旧协议私有根甲",
                            "旧协议私有根乙",
                            "旧协议私有根丙",
                            "旧协议私有根丁",
                        ],
                    );
                } else {
                    begin_historical_tree_topic(&mut stored, &["旧协议私有根目标"]);
                }
                let archive_nodes = match version {
                    3 => 0,
                    4 => 4,
                    _ => 1,
                };
                for index in 0..archive_nodes {
                    let output = node_output(
                        &stored,
                        json!([{"option_id":"a","goals":[format!("私有{version}-{index}甲执行")]},{"option_id":"b","goals":[format!("私有{version}-{index}乙执行")]}]),
                    );
                    apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
                }
                stored
                    .stage_workflow
                    .as_mut()
                    .unwrap()
                    .current_mut()
                    .topic_checkpoint
                    .as_mut()
                    .unwrap()
                    .preparation_contract_version = version;
                assert!(refresh_checkpoint_input(&mut stored, &request("status", 0)));
            }
            let prior_archives = serde_json::to_value(
                &stored
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .current()
                    .topic_checkpoint_archives,
            )
            .unwrap();
            begin_historical_tree_topic(
                &mut stored,
                &["旧根目标甲", "旧根目标乙", "旧根目标丙", "旧根目标丁"],
            );
            for index in 0..saved_nodes {
                let output = node_output(
                    &stored,
                    json!([{"option_id":"a","goals":[format!("旧分支{index}甲执行")]},{"option_id":"b","goals":[format!("旧分支{index}乙执行")]}]),
                );
                apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
            }
            stored.stage_workflow.as_mut().unwrap().chapters.clear();
            stored
                .stage_workflow
                .as_mut()
                .unwrap()
                .current_mut()
                .topic_checkpoint
                .as_mut()
                .unwrap()
                .preparation_contract_version = invalid_version;
            let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
            stage.preparation_contract_version = invalid_version;
            stage.root_axes_binding = Some(root_axes::load().unwrap());
            let original = serde_json::to_value(&stored).unwrap();
            let old_checkpoint = serde_json::to_value(checkpoint(&stored)).unwrap();
            assert_eq!(
                old_checkpoint["questions"].as_array().unwrap().len(),
                saved_nodes
            );
            assert_eq!(
                old_checkpoint["frontier"].as_array().unwrap().len(),
                pending_nodes
            );
            let old_generation = stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .generation_id
                .clone();
            let requests = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
            let seen = requests.clone();
            let release = Arc::new(tokio::sync::Notify::new());
            let resumed = release.clone();
            let router = axum::Router::new().route(
                "/creation/brainstorm/stage/batch",
                axum::routing::post(move |Json(body): Json<Value>| {
                    let seen = seen.clone();
                    let resumed = resumed.clone();
                    async move {
                        seen.lock().unwrap().push(body.clone());
                        if body["kind"] == "topic_plan" {
                            resumed.notified().await;
                            Json(compiled_topic_roots(&body))
                        } else {
                            std::future::pending::<Json<Value>>().await
                        }
                    }
                }),
            );
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
            let (_tmp, state) = fixture(Some(&stored), &url);
            let old_worker_key = format!("{SESSION}:{old_generation}");
            state
                .brainstorm_stage_jobs
                .lock()
                .unwrap()
                .insert(old_worker_key.clone(), tokio::spawn(std::future::pending()));
            let response = act(&state, &request("status", 0)).await.unwrap();
            assert_eq!(response.revision, 1);
            let public = serde_json::to_value(response).unwrap().to_string();
            for private in ["topic_checkpoint", "旧协议私有", "旧根目标", "旧分支"] {
                assert!(!public.contains(private));
            }
            assert!(!state
                .brainstorm_stage_jobs
                .lock()
                .unwrap()
                .contains_key(&old_worker_key));
            tokio::time::timeout(Duration::from_secs(2), async {
                while requests.lock().unwrap().is_empty() {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            let migrated = decode(&load(&state)).unwrap();
            let stage = migrated.stage_workflow.as_ref().unwrap().current();
            assert_eq!(
                stage.preparation_contract_version,
                PREPARATION_CONTRACT_VERSION
            );
            assert_ne!(stage.generation_id, old_generation);
            assert!(stage.questions.is_empty());
            assert!(stage.review_checkpoint.is_none());
            assert!(stage.topic_checkpoint.is_none());
            assert_eq!(stage.topic_checkpoint_archives.len(), prior_count + 1);
            assert_eq!(
                serde_json::to_value(&stage.topic_checkpoint_archives[..prior_count]).unwrap(),
                prior_archives
            );
            assert_eq!(
                stage.topic_checkpoint_archives[prior_count].reason,
                "contract_changed"
            );
            assert_eq!(
                serde_json::to_value(&stage.topic_checkpoint_archives[prior_count].checkpoint)
                    .unwrap(),
                old_checkpoint
            );
            assert_eq!(stage.progress.completed_batches, 1);
            assert_eq!(requests.lock().unwrap()[0]["kind"], "topic_plan");
            assert_eq!(requests.lock().unwrap()[0]["topic"]["id"], "topic1");
            assert!(!requests.lock().unwrap()[0].to_string().contains("旧分支"));
            assert!(persist(&state, SESSION, 0, "preparing", &stored).is_err());
            release.notify_one();
            tokio::time::timeout(Duration::from_secs(2), async {
                while requests.lock().unwrap().len() < 2 {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            let replanned = decode(&load(&state)).unwrap();
            let workflow = replanned.stage_workflow.as_ref().unwrap();
            let stage = workflow.current();
            let fresh = checkpoint(&replanned);
            assert_eq!(
                fresh.preparation_contract_version,
                PREPARATION_CONTRACT_VERSION
            );
            assert_ne!(fresh.build_id, old_checkpoint["build_id"].as_str().unwrap());
            assert!(fresh.questions.is_empty());
            assert_eq!(fresh.frontier.len(), stage.topics[0].root_axis_ids.len());
            assert_eq!(fresh.frontier[0].goal, canonical_root_goals("detail", 1)[0]);
            assert_eq!(
                serde_json::to_value(&stage.topic_checkpoint_archives).unwrap(),
                serde_json::to_value(
                    &migrated
                        .stage_workflow
                        .as_ref()
                        .unwrap()
                        .current()
                        .topic_checkpoint_archives
                )
                .unwrap()
            );
            assert_eq!(
                serde_json::to_value(&stage.topics).unwrap(),
                original["stage_workflow"]["stages"][1]["topics"]
            );
            assert_eq!(
                serde_json::to_value(&stage.preparation_archives).unwrap(),
                original["stage_workflow"]["stages"][1]["preparation_archives"]
            );
            assert_eq!(
                serde_json::to_value(&workflow.stages[0]).unwrap(),
                original["stage_workflow"]["stages"][0]
            );
            assert_eq!(
                serde_json::to_value(&workflow.stages[2]).unwrap(),
                original["stage_workflow"]["stages"][2]
            );
            assert_eq!(
                serde_json::to_value(&workflow.copy_repair_history).unwrap(),
                original["stage_workflow"]["copy_repair_history"]
            );
            assert_eq!(
                serde_json::to_value(&replanned.turns).unwrap(),
                original["turns"]
            );
            assert_eq!(replanned.root_request, stored.root_request);
            assert_eq!(replanned.brief_edits, stored.brief_edits);
            assert_eq!(replanned.user_input_revisions, stored.user_input_revisions);
            assert_eq!(replanned.selected_skills, stored.selected_skills);
            let calls = requests.lock().unwrap();
            assert_eq!(calls.len(), 2);
            assert_eq!(calls[1]["kind"], "node");
            assert_eq!(
                calls[1]["node"]["goal"],
                json!(canonical_root_goals("detail", 1)[0])
            );
            drop(calls);
            let public = serde_json::to_string(&workflow.for_response()).unwrap();
            assert!(!public.contains("topic_checkpoint"));
            assert!(!public.contains("旧根目标"));
            assert!(!public.contains("旧协议私有"));
            assert!(!public.contains("preparation_archives"));
            server.abort();
        }
    }

    #[tokio::test]
    async fn thinking_status_archives_v2_five_nodes_keeps_v0_archive_and_replans_only_current_topic(
    ) {
        let bodies = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
        let seen = bodies.clone();
        let release = Arc::new(tokio::sync::Notify::new());
        let resumed = release.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move |Json(body): Json<Value>| {
                let seen = seen.clone();
                let resumed = resumed.clone();
                async move {
                    seen.lock().unwrap().push(body.clone());
                    if body["kind"] == "topic_plan" {
                        resumed.notified().await;
                        Json(compiled_topic_roots(&body))
                    } else {
                        Json(http_node_output(&body))
                    }
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
        let mut stored = initial();
        apply_batch(
            &mut stored,
            json!({"topics":[
            {"id":"published","title":"已发布主题","goal":"既有有效决定"},
            {"id":"remaining","title":"当前主题","goal":"只处理当前主题具体决定"}]}),
        )
        .unwrap();
        apply_batch(&mut stored, four_question_tree()).unwrap();
        let root = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
        stored.turns.push(BrainstormStoredTurn {
            question: root.clone(),
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: "保持用户原始确认".into(),
                source: "user".into(),
            },
        });
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .copy_repair_history
            .push(CandidateCopyRepair {
                revision: 9,
                question_id: root.id,
                option_id: None,
                field: CandidateCopyField::WhyNow,
                old_text: "原说明".into(),
                new_text: "已审计说明".into(),
            });
        begin_historical_tree_topic(&mut stored, &["版本0私有旧目标"]);
        let old_output = node_output(
            &stored,
            json!([
            {"option_id":"a","goals":["版本0私有甲"]},{"option_id":"b","goals":["版本0私有乙"]}]),
        );
        apply_preparation_step(&mut stored, &request("status", 0), old_output).unwrap();
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .preparation_contract_version = 0;
        refresh_checkpoint_input(&mut stored, &request("status", 0));
        let old_archive = serde_json::to_value(
            &stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .topic_checkpoint_archives[0],
        )
        .unwrap();
        begin_historical_tree_topic(
            &mut stored,
            &["版本2私有根甲", "版本2私有根乙", "版本2私有根丙"],
        );
        for index in 0..5 {
            let next = if index < 3 {
                json!([
                {"option_id":"a","goals":[format!("版本2私有{index}甲决定")]},
                {"option_id":"b","goals":[format!("版本2私有{index}乙决定")]},
                {"option_id":"c","goals":[format!("版本2私有{index}丙决定")]}])
            } else {
                json!([])
            };
            let mut output = node_output(&stored, next);
            if index < 3 {
                output["question"]["options"].as_array_mut().unwrap().push(json!({"id":"c","label":"方案丙","description":"采用第三种独立组织方式","recommended":false}));
            }
            apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        }
        stored.stage_workflow.as_mut().unwrap().chapters.clear();
        stored
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .preparation_contract_version = 2;
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.preparation_contract_version = PREPARATION_CONTRACT_VERSION;
        stage.root_axes_binding = Some(root_axes::load().unwrap());
        let old_checkpoint = serde_json::to_value(checkpoint(&stored)).unwrap();
        assert_eq!(old_checkpoint["questions"].as_array().unwrap().len(), 5);
        assert_eq!(old_checkpoint["frontier"].as_array().unwrap().len(), 7);
        let prefix =
            serde_json::to_value(&stored.stage_workflow.as_ref().unwrap().current().questions)
                .unwrap();
        let answers = serde_json::to_value(&stored.turns).unwrap();
        let audit =
            serde_json::to_value(&stored.stage_workflow.as_ref().unwrap().copy_repair_history)
                .unwrap();
        let generation = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .generation_id
            .clone();
        let (_tmp, state) = fixture(Some(&stored), &url);
        let old_worker_key = format!("{SESSION}:{generation}");
        state
            .brainstorm_stage_jobs
            .lock()
            .unwrap()
            .insert(old_worker_key.clone(), tokio::spawn(std::future::pending()));
        let response = act(&state, &request("status", 0)).await.unwrap();
        assert_eq!(response.revision, 1);
        assert!(!state
            .brainstorm_stage_jobs
            .lock()
            .unwrap()
            .contains_key(&old_worker_key));
        let public = serde_json::to_value(response).unwrap();
        assert!(!public.to_string().contains("topic_checkpoint"));
        assert!(!public.to_string().contains("版本2私有"));
        tokio::time::timeout(Duration::from_secs(2), async {
            while bodies.lock().unwrap().is_empty() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let archived = decode(&load(&state)).unwrap();
        assert!(persist(&state, SESSION, 0, "preparing", &stored).is_err());
        assert_eq!(
            serde_json::to_value(decode(&load(&state)).unwrap()).unwrap(),
            serde_json::to_value(&archived).unwrap()
        );
        let stage = archived.stage_workflow.as_ref().unwrap().current();
        assert_ne!(stage.generation_id, generation);
        assert!(stage.topic_checkpoint.is_none());
        assert_eq!(stage.topic_checkpoint_archives.len(), 2);
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[0]).unwrap(),
            old_archive
        );
        assert_eq!(
            stage.topic_checkpoint_archives[1].reason,
            "contract_changed"
        );
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[1].checkpoint).unwrap(),
            old_checkpoint
        );
        assert_eq!(serde_json::to_value(&stage.questions).unwrap(), prefix);
        assert_eq!(serde_json::to_value(&archived.turns).unwrap(), answers);
        assert_eq!(
            serde_json::to_value(
                &archived
                    .stage_workflow
                    .as_ref()
                    .unwrap()
                    .copy_repair_history
            )
            .unwrap(),
            audit
        );
        assert_eq!(stage.progress.completed_batches, 2);
        let first = bodies.lock().unwrap()[0].clone();
        assert_eq!(first["kind"], "topic_plan");
        assert_eq!(first["topic"]["id"], "remaining");
        assert!(!first.to_string().contains("版本2私有"));
        release.notify_one();
        wait_stage(&state, "answering").await;
        let complete = decode(&load(&state)).unwrap();
        let stage = complete.stage_workflow.as_ref().unwrap().current();
        assert_eq!(serde_json::to_value(&stage.questions[..4]).unwrap(), prefix);
        assert_eq!(serde_json::to_value(&complete.turns).unwrap(), answers);
        assert_eq!(stage.topic_checkpoint_archives.len(), 2);
        // Topic proof + all three current atomic properties. Archived old
        // three-way branches remain complete and do not seed the new pool.
        assert_eq!(bodies.lock().unwrap().len(), 4);
        assert!(bodies
            .lock()
            .unwrap()
            .iter()
            .all(|body| body["topic"]["id"] == "remaining"));
        assert!(
            !serde_json::to_string(&complete.stage_workflow.as_ref().unwrap().for_response())
                .unwrap()
                .contains("版本2私有")
        );
        server.abort();
    }

    async fn wait_stage(state: &Arc<AppState>, status: &str) {
        tokio::time::timeout(Duration::from_secs(4), async {
            loop {
                if decode(&load(state))
                    .unwrap()
                    .stage_workflow
                    .unwrap()
                    .current()
                    .status
                    == status
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
    }

    #[tokio::test]
    async fn node_failure_restart_and_retry_reuse_paid_root_and_prior_topic_without_answer_changes()
    {
        let bodies = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
        let seen = bodies.clone();
        let first = Arc::new(std::sync::atomic::AtomicBool::new(true));
        let fail = first.clone();
        let router = axum::Router::new().route("/creation/brainstorm/stage/batch", axum::routing::post(
            move |Json(body): Json<Value>| {
                let seen = seen.clone(); let fail = fail.clone();
                async move {
                    seen.lock().unwrap().push(body.clone());
                    assert_eq!(body["kind"], "node");
                    if fail.swap(false, Ordering::SeqCst) {
                        return (StatusCode::GATEWAY_TIMEOUT, Json(json!({"detail":{"code":"BRAINSTORM_MODEL_TIMEOUT","retryable":true}}))).into_response();
                    }
                    Json(http_node_output(&body)).into_response()
                }
            }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let mut stored = initial();
        // Six eligible detail properties form two complete three-root topics.
        // Keep the prior paid tree and answer unchanged, then prepare only the
        // second topic with the real version15 atomic root wire contract.
        stored.stage_workflow.as_mut().unwrap().active_stage = "detail".into();
        stored.stage_workflow.as_mut().unwrap().current_mut().status = "thinking".into();
        let binding = root_axes::load().unwrap();
        let ids = binding
            .eligible("detail")
            .unwrap()
            .iter()
            .take(6)
            .map(|axis| axis["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        let topics = binding
            .compile_topics(
                "detail",
                &ids,
                2,
                BuildLimits::configured().max_questions_per_topic,
            )
            .unwrap();
        apply_batch(&mut stored, json!({"topics":topics,"chapters":[]})).unwrap();
        apply_batch(&mut stored, four_question_tree()).unwrap();
        let root = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
        stored.turns.push(BrainstormStoredTurn {
            question: root,
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: String::new(),
                source: "user".into(),
            },
        });
        stored.selected_skills = vec![json!({"id":"saved-skill"})];
        let prefix =
            serde_json::to_value(&stored.stage_workflow.as_ref().unwrap().current().questions)
                .unwrap();
        let answers = serde_json::to_value(&stored.turns).unwrap();
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            compiled_topic_roots(&payload),
        )
        .unwrap();
        assert_eq!(checkpoint(&stored).frontier.len(), 3);
        let output = node_output(&stored, json!([]));
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        assert_eq!(checkpoint(&stored).frontier.len(), 2);
        let saved_root = serde_json::to_value(&checkpoint(&stored).questions[0]).unwrap();
        assert_eq!(
            checkpoint(&stored).preparation_contract_version,
            PREPARATION_CONTRACT_VERSION
        );
        let build_id = checkpoint(&stored).build_id.clone();
        let generation = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .generation_id
            .clone();
        let (tmp, state) = fixture(Some(&stored), &url);
        act(&state, &request("status", 0)).await.unwrap();
        wait_stage(&state, "failed").await;
        let failed = load(&state);
        let failed_state = decode(&failed).unwrap();
        assert_eq!(
            serde_json::to_value(&checkpoint(&failed_state).questions[0]).unwrap(),
            saved_root
        );
        assert_eq!(
            failed_state
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .progress
                .completed_batches,
            2
        );
        assert_eq!(serde_json::to_value(&failed_state.turns).unwrap(), answers);

        // A new process state has no job cache; the durable checkpoint is enough.
        let reopened = Arc::new(AppState::with_service_urls(
            StorageManager::open(&tmp.path().join("stages.db")).unwrap(),
            url.clone(),
            url,
            vec![],
        ));
        let retry = act(&reopened, &request("retry_stage", failed.revision))
            .await
            .unwrap();
        assert!(retry
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .topic_checkpoint
            .is_none());
        wait_stage(&reopened, "answering").await;
        let complete = decode(&load(&reopened)).unwrap();
        let stage = complete.stage_workflow.as_ref().unwrap().current();
        assert!(stage.topic_checkpoint_archives.is_empty());
        assert_ne!(stage.generation_id, generation);
        assert_eq!(serde_json::to_value(&stage.questions[..4]).unwrap(), prefix);
        assert_eq!(
            serde_json::to_value(&stage.questions[4]).unwrap(),
            saved_root
        );
        assert!(stage.questions[4].id.contains(&build_id));
        assert_eq!(serde_json::to_value(&complete.turns).unwrap(), answers);
        assert_eq!(complete.selected_skills, stored.selected_skills);
        assert_eq!(stage.questions.len(), 7);
        assert!(stage.questions[4..]
            .iter()
            .all(|q| q.parent_question_id.is_none() && q.parent_option_id.is_none()));
        let requests = bodies.lock().unwrap();
        assert_eq!(requests.len(), 3);
        assert_eq!(requests[0]["node"]["id"], requests[1]["node"]["id"]);
        assert_eq!(requests[0]["context_id"], requests[2]["context_id"]);
        assert!(requests.iter().all(|body| body["node"]["depth"] == 1
            && body["node"]["max_next_goals"] == 0
            && body["node_mode"]["kind"] == "root_axis_atomic"));
        assert_ne!(requests[0]["node"]["id"], saved_root["id"]);
        assert_eq!(requests[1]["node"]["id"], stage.questions[5].id);
        assert_eq!(requests[2]["node"]["id"], stage.questions[6].id);
        server.abort();
    }

    #[tokio::test]
    async fn changed_permission_and_late_worker_cannot_publish_old_checkpoint_after_retry() {
        let bodies = Arc::new(std::sync::Mutex::new(Vec::<Value>::new()));
        let seen = bodies.clone();
        let release = Arc::new(tokio::sync::Notify::new());
        let delayed = release.clone();
        let calls = Arc::new(AtomicUsize::new(0));
        let count = calls.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move |Json(body): Json<Value>| {
                let seen = seen.clone();
                let delayed = delayed.clone();
                let count = count.clone();
                async move {
                    seen.lock().unwrap().push(body.clone());
                    if count.fetch_add(1, Ordering::SeqCst) == 0 {
                        delayed.notified().await;
                        let mut response = http_node_output(&body);
                        response["question"]["options"][0]["details"] =
                            json!("STALE_SOURCE_MUST_NOT_PUBLISH");
                        return Json(response);
                    }
                    if body["kind"] == "topic_plan" {
                        return Json(compiled_topic_roots(&body));
                    }
                    Json(http_node_output(&body))
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let mut stored = preparing_topic();
        begin_topic(&mut stored, &["撤权前设计"]);
        let old_build = checkpoint(&stored).build_id.clone();
        let old_context = checkpoint(&stored).input_fingerprint.clone();
        let (_tmp, state) = fixture(Some(&stored), &url);
        act(&state, &request("status", 0)).await.unwrap();
        tokio::time::timeout(Duration::from_secs(3), async {
            while bodies.lock().unwrap().is_empty() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        let session = load(&state);
        let mut changed = decode(&session).unwrap();
        changed.brief_edits.insert(
            "root_request".into(),
            "禁止使用历史记忆，只基于当前输入讨论".into(),
        );
        changed
            .user_input_revisions
            .insert("root_request".into(), 1);
        changed
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .status = "failed".into();
        persist(&state, SESSION, session.revision, "preparing", &changed).unwrap();
        act(&state, &request("retry_stage", session.revision + 1))
            .await
            .unwrap();
        release.notify_one();
        wait_stage(&state, "answering").await;
        let complete = decode(&load(&state)).unwrap();
        let serialized = serde_json::to_string(&complete).unwrap();
        assert!(!serialized.contains("STALE_SOURCE_MUST_NOT_PUBLISH"));
        let stage = complete.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.topic_checkpoint_archives.len(), 1);
        assert_eq!(stage.topic_checkpoint_archives[0].reason, "input_changed");
        assert_eq!(
            stage.topic_checkpoint_archives[0].checkpoint.build_id,
            old_build
        );
        assert!(
            !serde_json::to_string(&complete.stage_workflow.as_ref().unwrap().for_response())
                .unwrap()
                .contains(&old_build)
        );
        assert_eq!(complete.brief_edits, changed.brief_edits);
        let requests = bodies.lock().unwrap();
        assert!(requests
            .iter()
            .skip(1)
            .all(|body| body["context_id"] != old_context));
        assert_eq!(requests[1]["kind"], "topic_plan");
        server.abort();
    }

    #[test]
    fn candidate_copy_completeness_guard_rejects_clear_fragments_without_rejecting_titles() {
        for text in [
            "截至2026-08-",
            "期限2026-8-，须补充",
            "闭环落地进展与",
            "比较策略以及。",
            "选择甲或者）",
            "2026年至",
            "8月17日至",
            "１０％至",
        ] {
            assert!(!candidate_copy_is_complete(text), "{text}");
        }
        for text in [
            "知识普及",
            "数据总和",
            "团队参与。",
            "无微不至",
            "2026",
            "2026-08",
            "模型v2026-08-x",
            "限定2026-08-23",
            "夏至",
            "冬至",
            "截至冬至",
            "赠与",
            "授与",
            "付与",
            "施与",
            "相与",
            "给与",
        ] {
            assert!(candidate_copy_is_complete(text), "{text}");
        }
    }

    #[test]
    fn candidate_copy_invalid_patches_are_atomic_and_cannot_change_confirmed_paths() {
        for failure_kind in [
            "stale_text",
            "duplicate_target",
            "answered",
            "answered_descendant",
            "conflicting_labels",
            "blank",
            "dangling_date",
            "dangling_connective",
            "long_why",
            "why_has_option",
            "label_has_no_option",
            "child_anchor",
            "wrong_stage",
            "empty",
            "too_many",
        ] {
            let mut stored = ready();
            let questions = stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .questions
                .clone();
            let mut req = request("repair_candidate_copy", 0);
            req.candidate_copy_patches = vec![
                label_patch(&questions[1], 0, "集中组织策略"),
                why_patch(&questions[2], "候选仍待确认，需比较当前方案的取舍"),
            ];
            match failure_kind {
                "stale_text" => req.candidate_copy_patches[1].expected_text = "已过期文本".into(),
                "duplicate_target" => req
                    .candidate_copy_patches
                    .push(req.candidate_copy_patches[0].clone()),
                "answered" | "answered_descendant" => {
                    stored.turns.push(BrainstormStoredTurn {
                        question: questions[1].clone(),
                        answer: BrainstormAnswer {
                            selected_option_ids: vec!["a".into()],
                            custom_text: String::new(),
                            source: "user".into(),
                        },
                    });
                    if failure_kind == "answered_descendant" {
                        req.candidate_copy_patches[0] =
                            why_patch(&questions[0], "当前候选路线仍待确认");
                    }
                }
                "conflicting_labels" => req.candidate_copy_patches[0].text = " 方 案 乙 ".into(),
                "blank" => req.candidate_copy_patches[1].text = " \n ".into(),
                "dangling_date" => req.candidate_copy_patches[0].text = "截至2026-08-".into(),
                "dangling_connective" => {
                    req.candidate_copy_patches[0].text = "闭环落地进展与".into()
                }
                "long_why" => req.candidate_copy_patches[1].text = "长".repeat(81),
                "why_has_option" => req.candidate_copy_patches[1].option_id = Some("a".into()),
                "label_has_no_option" => req.candidate_copy_patches[0].option_id = None,
                "child_anchor" => {
                    req.candidate_copy_patches[0] = label_patch(&questions[0], 0, "集中组织策略")
                }
                "wrong_stage" => {
                    stored.stage_workflow.as_mut().unwrap().current_mut().status = "ready".into()
                }
                "empty" => req.candidate_copy_patches.clear(),
                "too_many" => {
                    req.candidate_copy_patches = vec![req.candidate_copy_patches[0].clone(); 17]
                }
                _ => unreachable!(),
            }
            let before = serde_json::to_value(&stored).unwrap();
            assert!(
                repair_candidate_copy(&mut stored, &req, 1).is_err(),
                "{failure_kind}"
            );
            assert_eq!(
                serde_json::to_value(&stored).unwrap(),
                before,
                "{failure_kind}"
            );
        }
    }

    #[test]
    fn candidate_why_repair_syncs_current_snapshot_and_legacy_audit_field_defaults_empty() {
        let mut stored = ready();
        let question = stored.current_question.as_ref().unwrap().clone();
        let mut req = request("repair_candidate_copy", 0);
        req.candidate_copy_patches = vec![why_patch(
            &question,
            "汇报对象尚未确认，先比较适合不同受众的候选呈现方式",
        )];
        assert_eq!(
            repair_candidate_copy(&mut stored, &req, 1).unwrap(),
            "exploring"
        );
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(
            stored.current_question.as_ref().unwrap().why_now,
            stage.questions[0].why_now
        );
        let audit = &stored.stage_workflow.as_ref().unwrap().copy_repair_history[0];
        assert_eq!(audit.field, CandidateCopyField::WhyNow);
        assert_eq!(audit.option_id, None);
        assert_eq!(audit.revision, 1);
        let mut legacy = serde_json::to_value(stored.stage_workflow.as_ref().unwrap()).unwrap();
        legacy
            .as_object_mut()
            .unwrap()
            .remove("copy_repair_history");
        let workflow: StageWorkflow = serde_json::from_value(legacy).unwrap();
        assert!(workflow.copy_repair_history.is_empty());
    }

    #[tokio::test]
    async fn candidate_copy_two_patch_commit_preserves_graph_answers_progress_history_and_never_calls_model(
    ) {
        let calls = Arc::new(AtomicUsize::new(0));
        let observed = calls.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move || {
                let calls = observed.clone();
                async move {
                    calls.fetch_add(1, Ordering::SeqCst);
                    StatusCode::INTERNAL_SERVER_ERROR
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let mut stored = ready();
        stored.selected_skills = vec![json!({"id":"preserved-skill","steps":["分析"]})];
        let root = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: root,
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: String::new(),
                source: "user".into(),
            },
        });
        select_next(&mut stored);
        let stage = stored.stage_workflow.as_mut().unwrap().current_mut();
        stage.questions[1].options[0].label = "截至2026-08-".into();
        stage.questions[2].options[0].label = "闭环落地进展与".into();
        stage.questions[1].options[0].details = "已验证来源与原文保持".into();
        stored.current_question = Some(stage.questions[1].clone());
        let mut req = request("repair_candidate_copy", 0);
        req.candidate_copy_patches = vec![
            label_patch(&stage.questions[1], 0, "限定8月17日至23日"),
            label_patch(&stage.questions[2], 0, "展示闭环落地进展"),
        ];
        let mut expected = serde_json::to_value(&stored).unwrap();
        expected["stage_workflow"]["stages"][0]["questions"][1]["options"][0]["label"] =
            json!("限定8月17日至23日");
        expected["stage_workflow"]["stages"][0]["questions"][2]["options"][0]["label"] =
            json!("展示闭环落地进展");
        expected["current_question"]["options"][0]["label"] = json!("限定8月17日至23日");
        expected["stage_workflow"]["copy_repair_history"] = json!([
            {"revision":1,"question_id":req.candidate_copy_patches[0].question_id,"option_id":"a","field":"label","old_text":"截至2026-08-","new_text":"限定8月17日至23日"},
            {"revision":1,"question_id":req.candidate_copy_patches[1].question_id,"option_id":"a","field":"label","old_text":"闭环落地进展与","new_text":"展示闭环落地进展"}]);
        let (_tmp, state) = fixture(Some(&stored), &url);
        let history_id = state
            .storage
            .with_conn(|conn| {
                Ok(creation_history::insert(
                    conn,
                    &stored.root_request,
                    "# 已有周报\n\n正文和输入必须保留。",
                    None,
                    None,
                    0,
                    None,
                    None,
                    None,
                    Some(SESSION),
                    Some("[]"),
                    Some("[]"),
                    None,
                    Some(&stored.root_request),
                    None,
                    1,
                    "initial",
                    None,
                )?)
            })
            .unwrap();
        let before_history = state
            .storage
            .with_conn(|conn| {
                Ok(serde_json::to_value(
                    creation_history::get_by_id(conn, history_id)?.unwrap(),
                )?)
            })
            .unwrap();
        let response = act(&state, &req).await.unwrap();
        assert_eq!(response.revision, 1);
        assert_eq!(response.phase, "exploring");
        assert_eq!(
            serde_json::to_value(decode(&load(&state)).unwrap()).unwrap(),
            expected
        );
        assert_eq!(
            response.current_question.unwrap().options[0].label,
            "限定8月17日至23日"
        );
        let after_history = state
            .storage
            .with_conn(|conn| {
                Ok(serde_json::to_value(
                    creation_history::get_by_id(conn, history_id)?.unwrap(),
                )?)
            })
            .unwrap();
        assert_eq!(before_history, after_history);
        assert!(state.brainstorm_stage_jobs.lock().unwrap().is_empty());
        assert_eq!(calls.load(Ordering::SeqCst), 0);
        let before_retry = load(&state);
        assert_eq!(
            act(&state, &req).await.unwrap_err().1 .0["code"],
            "BRAINSTORM_REVISION_CONFLICT"
        );
        assert_eq!(load(&state).state_json, before_retry.state_json);
        assert_eq!(load(&state).revision, before_retry.revision);
        server.abort();
    }
    fn catalog_initial() -> (
        BrainstormStoredState,
        crate::storage::repo::creation_skill::CreationSkillRecord,
    ) {
        let row = brainstorm_catalog::tests::record();
        let mut stored = initial();
        stored.selected_skills = vec![brainstorm_catalog::tests::selected(&row)];
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "detail".into();
        let stage = workflow.current_mut();
        stage.status = "thinking".into();
        stage.catalog_binding = brainstorm_catalog::from_record(&row, 8, 12, 2).unwrap();
        (stored, row)
    }
    fn catalog_plan(stored: &BrainstormStoredState) -> Value {
        let workflow = stored.stage_workflow.as_ref().unwrap();
        let topics = &workflow
            .current()
            .catalog_binding
            .as_ref()
            .unwrap()
            .stage(&workflow.active_stage)
            .unwrap()
            .topics;
        json!({"topics":topics.iter().map(|t|json!({"id":t.id,"title":t.title,"goal":t.goal})).collect::<Vec<_>>(),"chapters":[],"open_flags":[]})
    }
    fn catalog_topic_plan(stored: &BrainstormStoredState) -> Value {
        let workflow = stored.stage_workflow.as_ref().unwrap();
        let topic = current_topic(stored).unwrap();
        let configured = workflow
            .current()
            .catalog_binding
            .as_ref()
            .unwrap()
            .stage(&workflow.active_stage)
            .unwrap()
            .topics
            .iter()
            .find(|t| t.id == topic.id)
            .unwrap();
        json!({"root_goals":configured.root_slot_ids.iter().map(|id|brainstorm_catalog::slot_goal(id)).collect::<Vec<_>>()})
    }
    fn catalog_node(stored: &BrainstormStoredState) -> Value {
        let workflow = stored.stage_workflow.as_ref().unwrap();
        let checkpoint = checkpoint(stored);
        let node = &checkpoint.frontier[0];
        let slot = checkpoint
            .catalog_binding
            .as_ref()
            .unwrap()
            .slot(&workflow.active_stage, &node.goal)
            .unwrap();
        let topic = current_topic(stored).unwrap();
        let mut candidate: BrainstormQuestion =
            serde_json::from_value(question("ignored-model-id", None)).unwrap();
        candidate.parent_question_id = node.parent_question_id.clone();
        candidate.parent_option_id = node.parent_option_id.clone();
        let saved: Vec<_> = workflow
            .current()
            .questions
            .iter()
            .chain(&checkpoint.questions)
            .collect();
        let prefix = prepared_question_prefix(&candidate, &saved).unwrap();
        json!({"question":{"id":"ignored-model-id","dimension_id":topic.id,"dimension":topic.title,"type":"single_choice","prompt":format!("{prefix}{}",slot.prompt_body),"why_now":slot.why_now,"single_choice_reason":"各方法服务同一对象，请选择一种实施方式","required":true,"allow_custom":true,"answer_template":"补充你的具体实施方法。","context_details":"","options":slot.methods.iter().enumerate().map(|(index,m)|json!({"id":((b'a'+index as u8) as char).to_string(),"label":m.label,"description":m.description,"details":m.tradeoff,"recommended":index==0})).collect::<Vec<_>>()},"next_goals":slot.methods.iter().enumerate().filter(|(_,m)|!m.child_slot_ids.is_empty()).map(|(index,m)|json!({"option_id":((b'a'+index as u8) as char).to_string(),"goals":m.child_slot_ids.iter().map(|id|brainstorm_catalog::slot_goal(id)).collect::<Vec<_>>()})).collect::<Vec<_>>()})
    }
    #[test]
    fn trusted_catalog_compiles_complete_fifteen_node_tree_with_shallow_terminals() {
        let (mut stored, _) = catalog_initial();
        let req = request("status", 0);
        let plan = catalog_plan(&stored);
        apply_preparation_step(&mut stored, &req, plan).unwrap();
        let mut accepted_ids = vec![];
        for _ in 0..5 {
            let plan = catalog_topic_plan(&stored);
            apply_preparation_step(&mut stored, &req, plan).unwrap();
            while stored
                .stage_workflow
                .as_ref()
                .unwrap()
                .current()
                .topic_checkpoint
                .is_some()
            {
                accepted_ids.push(checkpoint(&stored).frontier[0].id.clone());
                let payload = batch_payload(&req, &stored).unwrap();
                assert_eq!(payload["node"]["max_options"], 2);
                let output = catalog_node(&stored);
                apply_preparation_step(&mut stored, &req, output).unwrap();
            }
        }
        let workflow = stored.stage_workflow.as_ref().unwrap();
        let stage = workflow.current();
        assert_eq!(stage.questions.len(), 15);
        assert_eq!(stage.progress.completed_batches, 6);
        assert_eq!(stage.status, "answering");
        assert_eq!(
            stage
                .questions
                .iter()
                .map(|q| q.id.clone())
                .collect::<Vec<_>>(),
            accepted_ids
        );
        assert!(stage.questions.iter().all(|q| q.context_details.is_empty()));
        assert!(stored.turns.is_empty());
        assert!(stored.memory_brief.is_empty());
        let public = serde_json::to_string(&workflow.for_response()).unwrap();
        for hidden in [
            "catalog_binding",
            "catalog_sha256",
            "profile_sha256",
            "catalog_slot:",
            "topic_checkpoint",
        ] {
            assert!(!public.contains(hidden), "{hidden}");
        }
    }
    #[test]
    fn catalog_node_rejects_any_mutated_copy_edges_or_complete_tree_without_state_change() {
        let (mut stored, _) = catalog_initial();
        let req = request("status", 0);
        let plan = catalog_plan(&stored);
        apply_preparation_step(&mut stored, &req, plan).unwrap();
        let plan = catalog_topic_plan(&stored);
        apply_preparation_step(&mut stored, &req, plan).unwrap();
        let original = serde_json::to_value(&stored).unwrap();
        let node = catalog_node(&stored);
        for path in [
            "prompt",
            "why_now",
            "dimension",
            "single_choice_reason",
            "answer_template",
            "context_details",
        ] {
            let mut output = node.clone();
            output["question"][path] = json!("伪造或重新制定策略");
            assert!(apply_preparation_step(&mut stored, &req, output).is_err());
            assert_eq!(serde_json::to_value(&stored).unwrap(), original);
        }
        for field in ["label", "description", "details", "id"] {
            let mut output = node.clone();
            output["question"]["options"][0][field] = json!("伪造方法");
            assert!(apply_preparation_step(&mut stored, &req, output).is_err());
            assert_eq!(serde_json::to_value(&stored).unwrap(), original);
        }
        let mut output = node.clone();
        output["next_goals"][0]["goals"][0] = json!("catalog_slot:unbound");
        assert!(apply_preparation_step(&mut stored, &req, output).is_err());
        assert_eq!(serde_json::to_value(&stored).unwrap(), original);
        assert!(apply_preparation_step(&mut stored, &req, tree()).is_err());
        assert_eq!(serde_json::to_value(&stored).unwrap(), original);
        apply_preparation_step(&mut stored, &req, node).unwrap();
        let prefix = serde_json::to_value(&checkpoint(&stored).questions).unwrap();
        let mut reloaded: BrainstormStoredState =
            serde_json::from_value(serde_json::to_value(&stored).unwrap()).unwrap();
        refresh_checkpoint_input(&mut reloaded, &req);
        assert_eq!(
            serde_json::to_value(&checkpoint(&reloaded).questions).unwrap(),
            prefix
        );
    }
    #[test]
    fn contract_seven_archives_six_checkpoint_and_old_manifest_preserving_prior_archives() {
        let mut stored = initial();
        apply_batch(&mut stored, plan()).unwrap();
        begin_historical_tree_topic(
            &mut stored,
            &["未发布根甲", "未发布根乙", "未发布根丙", "未发布根丁"],
        );
        let output = node_output(
            &stored,
            json!([{"option_id":"a","goals":["未发布子甲"]},{"option_id":"b","goals":["未发布子乙"]}]),
        );
        apply_preparation_step(&mut stored, &request("status", 0), output).unwrap();
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.chapters.clear();
        workflow.active_stage = "detail".into();
        let old = workflow.stages[0].clone();
        workflow.stages[1] = Stage {
            id: "detail".into(),
            title: "细节".into(),
            ..old
        };
        let stage = workflow.current_mut();
        stage.preparation_contract_version = 6;
        stage
            .topic_checkpoint
            .as_mut()
            .unwrap()
            .preparation_contract_version = 6;
        for version in [0, 2, 3, 4, 5] {
            let mut cp = stage.topic_checkpoint.as_ref().unwrap().clone();
            cp.preparation_contract_version = version;
            stage
                .topic_checkpoint_archives
                .push(TopicCheckpointArchive {
                    reason: "contract_changed".into(),
                    archived_at_ms: 1,
                    checkpoint: cp,
                });
        }
        let original = serde_json::to_value(&stored).unwrap();
        let old_checkpoint = original["stage_workflow"]["stages"][1]["topic_checkpoint"].clone();
        assert_eq!(old_checkpoint["questions"].as_array().unwrap().len(), 1);
        assert_eq!(old_checkpoint["frontier"].as_array().unwrap().len(), 5);
        let row = brainstorm_catalog::tests::record();
        stored.selected_skills = vec![brainstorm_catalog::tests::selected(&row)];
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        brainstorm_catalog::tests::install(&state.storage, &row);
        assert!(refresh_catalog_binding(&state, &mut stored).unwrap());
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert!(stage.topic_checkpoint.is_none());
        assert!(stage.topics.is_empty());
        assert_eq!(
            stage.preparation_contract_version,
            PREPARATION_CONTRACT_VERSION
        );
        assert_eq!(stage.topic_checkpoint_archives.len(), 6);
        let archived = serde_json::to_value(&stage.topic_checkpoint_archives).unwrap();
        assert_eq!(
            archived.as_array().unwrap()[..5],
            original["stage_workflow"]["stages"][1]["topic_checkpoint_archives"]
                .as_array()
                .unwrap()[..]
        );
        assert_eq!(archived[5]["checkpoint"], old_checkpoint);
        assert_eq!(archived[5]["reason"], "contract_changed");
        assert_eq!(
            serde_json::to_value(&stage.preparation_archives[0].topics).unwrap(),
            original["stage_workflow"]["stages"][1]["topics"]
        );
        assert_eq!(stage.preparation_archives[0].reason, "catalog_changed");
        assert!(!refresh_catalog_binding(&state, &mut stored).unwrap());
        let public =
            serde_json::to_string(&stored.stage_workflow.as_ref().unwrap().for_response()).unwrap();
        assert!(!public.contains("未发布"));
        assert!(!public.contains("catalog_binding"));
    }
    #[test]
    fn catalog_binding_rechecked_before_persist_and_changed_hash_archives_paid_prefix() {
        let (mut stored, row) = catalog_initial();
        let req = request("status", 0);
        let plan = catalog_plan(&stored);
        apply_preparation_step(&mut stored, &req, plan).unwrap();
        let plan = catalog_topic_plan(&stored);
        apply_preparation_step(&mut stored, &req, plan).unwrap();
        let output = catalog_node(&stored);
        apply_preparation_step(&mut stored, &req, output).unwrap();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        brainstorm_catalog::tests::install(&state.storage, &row);
        let saved = serde_json::to_value(checkpoint(&stored)).unwrap();
        let before = load(&state);
        let mut changed = row.clone();
        let file = changed
            .package_files
            .iter_mut()
            .find(|f| f.path == "references/brainstorm-catalog.json")
            .unwrap();
        use base64::Engine;
        let mut bytes = base64::engine::general_purpose::STANDARD
            .decode(&file.content_base64)
            .unwrap();
        bytes.push(b' ');
        file.content_base64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
        file.size_bytes = bytes.len() as u64;
        brainstorm_catalog::tests::install(&state.storage, &changed);
        let error = persist(&state, SESSION, before.revision, "preparing", &stored).unwrap_err();
        assert!(error.1 .0.to_string().contains("当前技能脑暴配置已失效"));
        assert!(!error.1 .0.to_string().contains("sha256"));
        assert_eq!(load(&state).state_json, before.state_json);
        assert!(refresh_catalog_binding(&state, &mut stored).unwrap());
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(
            serde_json::to_value(&stage.topic_checkpoint_archives[0].checkpoint).unwrap(),
            saved
        );
        assert!(stage.topics.is_empty());
        assert_eq!(stage.topic_checkpoint_archives[0].reason, "input_changed");
        changed.installed = false;
        brainstorm_catalog::tests::install(&state.storage, &changed);
        let before = serde_json::to_value(&stored).unwrap();
        assert!(refresh_catalog_binding(&state, &mut stored).is_err());
        assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        let mut protected = ready();
        protected
            .stage_workflow
            .as_mut()
            .unwrap()
            .current_mut()
            .catalog_binding = Some(
            brainstorm_catalog::from_record(&row, 8, 12, 2)
                .unwrap()
                .unwrap(),
        );
        protected.turns.push(BrainstormStoredTurn {
            question: protected.current_question.clone().unwrap(),
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                ..Default::default()
            },
        });
        assert!(!refresh_catalog_binding(&state, &mut protected).unwrap());
    }

    fn fixture(
        stored: Option<&BrainstormStoredState>,
        url: &str,
    ) -> (tempfile::TempDir, Arc<AppState>) {
        let tmp = tempfile::tempdir().unwrap();
        let storage = StorageManager::open(&tmp.path().join("stages.db")).unwrap();
        let state = AppState::with_service_urls(storage, url.into(), url.into(), vec![]);
        if let Some(stored) = stored {
            state
                .storage
                .with_conn(|conn| {
                    repo::create(
                        conn,
                        SESSION,
                        &stored.root_request,
                        "exploring",
                        &serde_json::to_string(stored)?,
                    )?;
                    Ok(())
                })
                .unwrap();
        }
        (tmp, state)
    }
    fn load(state: &AppState) -> repo::CreationBrainstormSession {
        state
            .storage
            .with_conn(|conn| Ok(repo::get(conn, SESSION)?.unwrap()))
            .unwrap()
    }
    async fn act(
        state: &Arc<AppState>,
        req: &BrainstormTurnRequest,
    ) -> Result<BrainstormTurnResponse, Failure> {
        let session = state
            .storage
            .with_conn(|conn| Ok(repo::get(conn, SESSION)?))
            .unwrap();
        turn(state.clone(), req, session).await.map(|r| r.0)
    }

    #[test]
    fn input_preflight_questions_are_saved_before_detail_catalog_and_never_preselected() {
        let mut stored = initial();
        let stage = stored.stage_workflow.as_mut().unwrap();
        stage.active_stage = "detail".into();
        stage.current_mut().input_phase = "pending".into();
        let question = json!({
            "id":"input_report_period_case", "dimension_id":"report_period",
            "dimension":"报告周期", "type":"single_choice",
            "prompt":"这份周报应覆盖哪一周？", "why_now":"先确认范围",
            "required":true, "allow_custom":true,
            "options":[
                {"id":"previous","label":"最近完整一周","description":"2026-09-21 至 2026-09-27",
                 "recommended":false,"input_value":{"start":"2026-09-21","end":"2026-09-27","timezone":"Asia/Shanghai"}},
                {"id":"current","label":"本周","description":"2026-09-28 至 2026-10-04",
                 "recommended":false,"input_value":{"start":"2026-09-28","end":"2026-10-04","timezone":"Asia/Shanghai"}}
            ],
            "input_clarification":{"kind":"report_period","need_ids":["need-a"]}
        });
        let phase = apply_input_preflight(&mut stored, json!({
            "input_preflight":{"schema_version":1,"status":"partial",
                "as_of_local_date":"2026-09-28","timezone":"Asia/Shanghai",
                "period":{"status":"unconfirmed"},
                "needs":[{"id":"need-a","status":"candidate","receipts":[]}]},
            "input_plan_status":"question","questions":[question]
        })).unwrap();
        assert_eq!(phase, "exploring");
        assert_eq!(stored.current_question.as_ref().unwrap().id, "input_report_period_case");
        assert!(stored.turns.is_empty());
        assert_eq!(stored.stage_workflow.as_ref().unwrap().current().progress.completed_batches, 0);
        assert!(confirmed_answer_period(stored.current_question.as_ref().unwrap(),
            &BrainstormAnswer { selected_option_ids: vec!["previous".into()],
                source: "user".into(), ..Default::default() }, "Asia/Shanghai").is_ok());
        assert!(confirmed_answer_period(stored.current_question.as_ref().unwrap(),
            &BrainstormAnswer { custom_text: "本周".into(), source: "user".into(),
                ..Default::default() }, "Asia/Shanghai").is_err());
    }

    #[test]
    fn changing_report_period_archives_only_dependent_source_answers_and_rechecks_receipts() {
        let mut stored = initial();
        let period: BrainstormQuestion = serde_json::from_value(json!({
            "id":"input-period", "dimension":"周期", "type":"single_choice",
            "prompt":"哪一周？", "why_now":"核对来源", "required":true, "allow_custom":true,
            "options":[
                {"id":"old","label":"上一周","description":"旧范围","recommended":false,
                 "input_value":{"start":"2026-09-21","end":"2026-09-27","timezone":"Asia/Shanghai"}},
                {"id":"new","label":"这一周","description":"新范围","recommended":false,
                 "input_value":{"start":"2026-09-28","end":"2026-10-04","timezone":"Asia/Shanghai"}}
            ], "input_clarification":{"kind":"report_period","need_ids":["need-one"]}
        })).unwrap();
        let source: BrainstormQuestion = serde_json::from_value(json!({
            "id":"input-source", "dimension":"资料", "type":"single_choice",
            "prompt":"来源如何确认？", "why_now":"来源待核验", "required":true, "allow_custom":true,
            "options":[{"id":"gap","label":"标明缺口","description":"不冒充事实","recommended":false,
                "input_value":{"action":"draft_with_gap"}},
                {"id":"retry","label":"重查","description":"继续检索","recommended":false,
                "input_value":{"action":"retry_local_recall"}}],
            "input_clarification":{"kind":"source_confirmation","need_ids":["need-one"]}
        })).unwrap();
        let stage = stored.stage_workflow.as_mut().unwrap();
        stage.active_stage = "detail".into();
        stage.current_mut().questions = vec![period.clone(), source.clone()];
        stage.current_mut().status = "ready".into();
        stage.current_mut().input_phase = "complete".into();
        stage.current_mut().confirmed_period = Some(json!({"start":"2026-09-21",
            "end":"2026-09-27","source":"user"}));
        stage.current_mut().input_preflight = Some(json!({"timezone":"Asia/Shanghai"}));
        for (question, option) in [(period, "old"), (source, "gap")] {
            stored.turns.push(BrainstormStoredTurn { question,
                answer: BrainstormAnswer { selected_option_ids: vec![option.into()],
                    source:"user".into(), ..Default::default() } });
        }
        let mut req = request("revise_answer", 2);
        req.question_id = Some("input-period".into());
        req.answer = Some(BrainstormAnswer { selected_option_ids: vec!["new".into()],
            source:"user".into(), ..Default::default() });
        assert_eq!(revise_saved_answer(&mut stored, &req, 3).unwrap(), "preparing");
        assert_eq!(stored.turns.len(), 1);
        assert_eq!(stored.archived_questions.iter().filter(|item| item.question.id == "input-source").count(), 1);
        let detail = stored.stage_workflow.as_ref().unwrap().current();
        assert_eq!(detail.input_phase, "pending");
        assert_eq!(detail.confirmed_period.as_ref().unwrap()["start"], "2026-09-28");
    }

    #[tokio::test]
    async fn review_inputs_reopens_original_detail_without_erasing_answers_or_operation() {
        let mut stored = initial();
        stored.root_request = "请生成GPU成本优化的周报".into();
        let template = serde_json::from_value::<BrainstormQuestion>(json!({
            "id":"old-1","dimension":"原有决定","type":"single_choice",
            "prompt":"原有选题","why_now":"原有理由","required":true,"allow_custom":true,
            "options":[{"id":"a","label":"保留","description":"用户已选","recommended":true},
                {"id":"b","label":"其它","description":"另一方案","recommended":false}]
        })).unwrap();
        for index in 0..17 {
            let mut question = template.clone();
            question.id = format!("old-{index}");
            stored.turns.push(BrainstormStoredTurn { question,
                answer: BrainstormAnswer { selected_option_ids: vec!["a".into()],
                    source: "user".into(), ..Default::default() } });
        }
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.stages[1].status = "completed".into();
        workflow.active_stage = "writing".into();
        workflow.stages[2].status = "ready".into();
        workflow.active_chapter_id = Some("chapter-one".into());
        workflow.chapters = vec![Chapter { id:"chapter-one".into(), title:"正文".into(),
            goal:"写周报".into(), status:"writing".into(), question_ids:Vec::new(),
            instruction_id:Some("old-instruction".into()), operation_id:Some("old-operation".into()) }];
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let result = act(&state, &request("review_inputs", 0)).await.unwrap();
        assert_eq!(result.stage_workflow.as_ref().unwrap().active_stage, "detail");
        let saved = decode(&load(&state)).unwrap();
        assert_eq!(saved.turns.len(), 17);
        assert_eq!(saved.stage_workflow.as_ref().unwrap().chapters[0].operation_id.as_deref(),
            Some("old-operation"));
        assert_eq!(saved.stage_workflow.as_ref().unwrap().current().input_phase, "pending");
    }

    #[tokio::test]
    async fn refresh_inputs_rechecks_sources_without_answering_or_erasing_turns() {
        let mut stored = initial();
        stored.root_request = "请生成GPU成本优化的周报".into();
        let question: BrainstormQuestion = serde_json::from_value(json!({
            "id":"input-period", "dimension":"报告周期", "type":"single_choice",
            "prompt":"这份周报应覆盖哪个周期？", "why_now":"先确认周期",
            "required":true, "allow_custom":true,
            "options":[{"id":"previous","label":"最近完整一周","description":"起止日",
                "recommended":false,"input_value":{"start":"2026-09-21",
                "end":"2026-09-27","timezone":"Asia/Shanghai"}}],
            "input_clarification":{"kind":"report_period","need_ids":["need-one"]}
        })).unwrap();
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "detail".into();
        let detail = workflow.current_mut();
        detail.status = "answering".into();
        detail.input_phase = "questions".into();
        detail.input_preflight = Some(json!({"schema_version":1,"timezone":"Asia/Shanghai",
            "period":{"status":"unconfirmed"},"needs":[{"id":"need-one",
            "status":"candidate","receipts":[]}]}));
        detail.questions.push(question.clone());
        stored.current_question = Some(question);
        stored.turns.push(BrainstormStoredTurn { question: serde_json::from_value(json!({
            "id":"old-answer","dimension":"原有决定","type":"single_choice",
            "prompt":"原有题","why_now":"原有原因","required":true,"allow_custom":true,
            "options":[{"id":"old","label":"保留","description":"原回答","recommended":false}]
        })).unwrap(), answer: BrainstormAnswer { selected_option_ids: vec!["old".into()],
            source:"user".into(), ..Default::default() } });
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let result = act(&state, &request("refresh_inputs", 0)).await.unwrap();
        assert_eq!(result.phase, "preparing");
        assert!(result.current_question.is_none());
        let saved = decode(&load(&state)).unwrap();
        assert_eq!(saved.turns.len(), 1);
        let detail = saved.stage_workflow.as_ref().unwrap().current();
        assert_eq!(detail.input_attempt, 1);
        assert_eq!(detail.input_phase, "pending");
        assert_eq!(detail.status, "thinking");
    }

    #[tokio::test]
    async fn legacy_detail_confirmation_starts_preflight_instead_of_blocking() {
        let mut stored = initial();
        stored.root_request = "请生成GPU成本优化的周报".into();
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "detail".into();
        workflow.current_mut().status = "ready".into();
        assert!(workflow.current().input_preflight.is_none());
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let result = act(&state, &request("confirm_stage", 0)).await.unwrap();
        assert_eq!(result.phase, "preparing");
        let saved = decode(&load(&state)).unwrap();
        let workflow = saved.stage_workflow.as_ref().unwrap();
        assert_eq!(workflow.active_stage, "detail");
        assert_eq!(workflow.current().input_phase, "pending");
        assert_eq!(workflow.current().status, "thinking");
    }

    #[tokio::test]
    async fn start_saves_work_context_once_and_resume_preserves_confirmed_answers() {
        let context = json!({"purpose":"评审试点", "constraints":"保留已有人员", "style_samples":["只学习写法的虚构样稿"]});
        let (_new_tmp, fresh) = fixture(None, "http://127.0.0.1:9");
        let mut start = request("start", 0);
        start.work_context = Some(context.clone());
        let result = act(&fresh, &start).await.unwrap();
        assert_eq!(result.work_context.as_ref().unwrap()["purpose"], "评审试点");
        assert_eq!(decode(&load(&fresh)).unwrap().skill_binding_version, 1);
        assert_eq!(
            decode(&load(&fresh)).unwrap().work_context,
            result.work_context
        );

        let mut stored = ready();
        stored.work_context = brainstorm_initial_work_context(&start).unwrap();
        let question = stored.stage_workflow.as_ref().unwrap().current().questions[0].clone();
        stored.turns.push(BrainstormStoredTurn {
            question,
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: "已确认的选择".into(),
                source: "user".into(),
            },
        });
        let confirmed = serde_json::to_value(&stored.turns).unwrap();
        let (_existing_tmp, existing) = fixture(Some(&stored), "http://127.0.0.1:9");
        start.work_context = Some(json!({"purpose":"新的写作用途"}));
        let resumed = act(&existing, &start).await.unwrap();
        assert_eq!(resumed.work_context, stored.work_context);
        assert_eq!(
            serde_json::to_value(&decode(&load(&existing)).unwrap().turns).unwrap(),
            confirmed
        );
    }
    fn selected(
        question: &BrainstormQuestion,
        option: &str,
        revision: i64,
    ) -> BrainstormTurnRequest {
        let mut req = request("answer", revision);
        req.question_id = Some(question.id.clone());
        req.answer = Some(BrainstormAnswer {
            selected_option_ids: vec![option.into()],
            custom_text: String::new(),
            source: "user".into(),
        });
        req
    }

    fn current_design_after_completed_chapter() -> BrainstormStoredState {
        let mut stored = ready();
        let direction = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: direction,
            answer: BrainstormAnswer {
                selected_option_ids: vec![],
                custom_text: "已确认全篇方向".into(),
                source: "user".into(),
            },
        });
        let w = stored.stage_workflow.as_mut().unwrap();
        w.stages[0].status = "completed".into();
        w.stages[1].status = "completed".into();
        w.active_stage = "writing".into();
        apply_batch(
            &mut stored,
            json!({"topics":[
            {"id":"one","title":"第一章","goal":"首章设计","chapter_id":"one"},
            {"id":"two","title":"第二章","goal":"次章设计","chapter_id":"two"}],
            "chapters":[{"id":"one","title":"开篇","goal":"说明目标"},
                {"id":"two","title":"结尾","goal":"说明行动"}]}),
        )
        .unwrap();
        apply_batch(&mut stored, tree()).unwrap();
        apply_batch(&mut stored, tree_for_topic("结尾章节")).unwrap();
        let first = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: first,
            answer: BrainstormAnswer {
                selected_option_ids: vec![],
                custom_text: "已完成首章设计".into(),
                source: "user".into(),
            },
        });
        let w = stored.stage_workflow.as_mut().unwrap();
        w.chapters[0].status = "completed".into();
        w.chapters[0].instruction_id = Some("first-chapter-instruction".into());
        w.chapters[0].operation_id = Some("first-chapter-operation".into());
        w.active_chapter_id = Some("two".into());
        select_next(&mut stored);
        for _ in 0..2 {
            let question = stored.current_question.take().unwrap();
            stored.turns.push(BrainstormStoredTurn {
                question,
                answer: BrainstormAnswer {
                    selected_option_ids: vec!["a".into()],
                    custom_text: String::new(),
                    source: "user".into(),
                },
            });
            select_next(&mut stored);
        }
        stored
    }

    #[tokio::test]
    async fn current_unwritten_chapter_can_be_revised_after_previous_chapter_completion() {
        let stored = current_design_after_completed_chapter();
        let w = stored.stage_workflow.as_ref().unwrap();
        let previous = serde_json::to_value(&w.chapters[0]).unwrap();
        let original_tree = serde_json::to_value(&w.current().questions).unwrap();
        let active_root = w
            .current()
            .questions
            .iter()
            .find(|q| w.chapters[1].question_ids.contains(&q.id) && q.parent_question_id.is_none())
            .unwrap()
            .clone();
        let abandoned_child = stored.turns.last().unwrap().question.id.clone();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let mut req = selected(&active_root, "b", 0);
        req.action = "revise_answer".into();
        let revised = act(&state, &req).await.unwrap();
        assert_eq!(
            revised
                .current_question
                .as_ref()
                .unwrap()
                .parent_option_id
                .as_deref(),
            Some("b")
        );
        assert!(revised
            .archived_questions
            .iter()
            .any(|q| q.question.id == abandoned_child));
        let workflow = revised.stage_workflow.unwrap();
        assert_eq!(
            serde_json::to_value(&workflow.chapters[0]).unwrap(),
            previous
        );
        assert_eq!(
            serde_json::to_value(&workflow.current().questions).unwrap(),
            original_tree
        );
        assert_eq!(workflow.active_chapter_id.as_deref(), Some("two"));
        assert_eq!(workflow.chapters[1].status, "answering");

        let mut reopen = request("reopen", revised.revision);
        reopen.question_id = Some(active_root.id.clone());
        let reopened = act(&state, &reopen).await.unwrap();
        assert_eq!(reopened.current_question.unwrap().id, active_root.id);
        assert_eq!(
            reopened.stage_workflow.unwrap().chapters[1].status,
            "answering"
        );
        let snapshot = load(&state).state_json;
        for question in stored.turns.iter().take(2).map(|t| &t.question) {
            let mut blocked = selected(question, "a", reopened.revision);
            blocked.action = "revise_answer".into();
            let (status, Json(error)) = act(&state, &blocked).await.unwrap_err();
            assert_eq!(status, StatusCode::CONFLICT);
            assert_eq!(error["code"], "BRAINSTORM_CHAPTER_REVIEW_REQUIRED");
            assert_eq!(load(&state).state_json, snapshot);
        }
        assert!(state.brainstorm_stage_jobs.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn current_chapter_brief_edits_and_clears_keep_completed_body_and_upstream_frozen() {
        let stored = current_design_after_completed_chapter();
        let w = stored.stage_workflow.as_ref().unwrap();
        let previous = serde_json::to_value(&w.chapters[0]).unwrap();
        let current_root = w.chapters[1].question_ids[0].clone();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let body = "# 已完成第一章\n\n用户已确认的正文，须保留。";
        state
            .storage
            .with_conn(|conn| {
                creation_history::insert(
                    conn,
                    &stored.root_request,
                    body,
                    None,
                    None,
                    0,
                    None,
                    None,
                    None,
                    Some(SESSION),
                    Some("[]"),
                    Some("[]"),
                    None,
                    Some(&stored.root_request),
                    None,
                    1,
                    "initial",
                    None,
                )?;
                Ok(())
            })
            .unwrap();
        let mut edit = request("edit_brief", 0);
        // Saving the full displayed brief must not mistake unchanged upstream
        // fields for changes to already written chapters.
        edit.brief_edits
            .insert("root_request".into(), stored.root_request.clone());
        edit.brief_edits
            .insert("open_flags".into(), stored.open_flags.join("\n"));
        for turn in stored.turns.iter().take(2) {
            edit.brief_edits.insert(
                turn.question.id.clone(),
                brainstorm_effective_summary(&stored, turn),
            );
        }
        edit.brief_edits.insert(
            current_root.clone(),
            "本章改为读者可以立即采用的建议".into(),
        );
        let updated = act(&state, &edit).await.unwrap();
        assert_eq!(updated.phase, "ready");
        let mut clear = request("edit_brief", updated.revision);
        clear
            .brief_edits
            .insert(current_root.clone(), String::new());
        let cleared = act(&state, &clear).await.unwrap();
        assert_eq!(cleared.phase, "ready");
        assert!(cleared.current_question.is_none());
        assert!(cleared
            .decisions
            .iter()
            .all(|d| d.question_id != current_root));
        assert_eq!(
            serde_json::to_value(&cleared.stage_workflow.unwrap().chapters[0]).unwrap(),
            previous
        );

        let snapshot = load(&state).state_json;
        for id in [
            "root_request".to_string(),
            "open_flags".into(),
            stored.turns[0].question.id.clone(),
            stored.turns[1].question.id.clone(),
        ] {
            let mut blocked = request("edit_brief", cleared.revision);
            blocked
                .brief_edits
                .insert(id, "不能改写此前已确认的内容".into());
            let (_, Json(error)) = act(&state, &blocked).await.unwrap_err();
            assert_eq!(error["code"], "BRAINSTORM_CHAPTER_REVIEW_REQUIRED");
            assert_eq!(load(&state).state_json, snapshot);
        }
        let saved_body = state
            .storage
            .with_conn(|conn| {
                Ok(creation_history::get_session_context(conn, SESSION)?
                    .unwrap()
                    .latest
                    .generated_content)
            })
            .unwrap();
        assert_eq!(saved_body, body);
        assert!(state.brainstorm_stage_jobs.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn history_summaries_distinguish_pending_chapters_from_completed_document_operations() {
        let mut stored = current_design_after_completed_chapter();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let history_id = state.storage.with_conn(|conn| {
            let id = creation_history::insert(conn, &stored.root_request, "# 已完成首章\n\n正文保持不变。",
                None, None, 0, None, None, None, Some(SESSION), Some("[]"), Some("[]"),
                Some("{\"status\":\"complete\"}"), Some(&stored.root_request), None, 1, "initial", None)?;
            creation_history::insert(conn, "原有普通会话", "普通正文", None, None, 0, None, None, None,
                Some("legacy-session"), None, None, None, None, None, 1, "initial", None)?;
            // This snapshot intentionally lacks stages, as persisted history
            // may predate the newest stage transaction. The summary is sourced
            // from the current authoritative workflow instead.
            conn.execute("UPDATE creation_history SET creation_mode='brainstorm',creation_brief_json='{}' WHERE id=?1", [id])?;
            Ok(id)
        }).unwrap();
        let original_history = state
            .storage
            .with_conn(|conn| {
                Ok(serde_json::to_value(
                    creation_history::get_by_id(conn, history_id)?.unwrap(),
                )?)
            })
            .unwrap();
        for (revision, phase, chapter_status, stage_status, expected_done) in [
            (0, "ready", "ready", "ready", 1),
            (1, "chapter_writing", "writing", "ready", 1),
            (2, "completed", "completed", "completed", 2),
        ] {
            let w = stored.stage_workflow.as_mut().unwrap();
            w.chapters[1].status = chapter_status.into();
            w.current_mut().status = stage_status.into();
            if phase == "completed" {
                w.active_chapter_id = None;
            }
            persist(&state, SESSION, revision, phase, &stored).unwrap();
            for paged in [false, true] {
                let response = list_history(
                    State(state.clone()),
                    Query(ListHistoryParams {
                        limit: Some(20),
                        offset: 0,
                        q: None,
                        paged,
                    }),
                )
                .await
                .unwrap();
                let body = axum::body::to_bytes(response.into_body(), 1024 * 1024)
                    .await
                    .unwrap();
                let response: Value = serde_json::from_slice(&body).unwrap();
                let items = if paged {
                    response["items"].as_array().unwrap()
                } else {
                    response.as_array().unwrap()
                };
                assert_eq!(items.len(), 2);
                let item = items.iter().find(|item| item["id"] == history_id).unwrap();
                let summary = &item["brainstorm_stage_summary"];
                assert_eq!(summary["phase"], phase);
                assert_eq!(summary["completed_chapters"], expected_done);
                assert_eq!(summary["total_chapters"], 2);
                assert_eq!(item["lifecycle_status"], "completed");
                assert_eq!(item["goal_json"], "{\"status\":\"complete\"}");
                assert_eq!(item["creation_brief_json"], "{}");
                assert!(item.get("history").is_none());
                assert!(items
                    .iter()
                    .find(|item| item["session_id"] == "legacy-session")
                    .unwrap()
                    .get("brainstorm_stage_summary")
                    .is_none());
            }
            let response = get_history(State(state.clone()), Path(history_id))
                .await
                .unwrap();
            let body = axum::body::to_bytes(response.into_body(), 1024 * 1024)
                .await
                .unwrap();
            let response: Value = serde_json::from_slice(&body).unwrap();
            assert_eq!(response["brainstorm_stage_summary"]["phase"], phase);
            assert_eq!(
                response["generated_content"],
                original_history["generated_content"]
            );
        }
        let after = state
            .storage
            .with_conn(|conn| {
                Ok(serde_json::to_value(
                    creation_history::get_by_id(conn, history_id)?.unwrap(),
                )?)
            })
            .unwrap();
        assert_eq!(after, original_history);
        assert!(state.brainstorm_stage_jobs.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn failed_output_validation_allows_explicit_retry_without_replaying_saved_batches() {
        for failure_kind in [
            "invalid_output",
            "legacy_questions_response",
            "permission",
            "input",
        ] {
            let count = Arc::new(AtomicUsize::new(0));
            let observed = count.clone();
            let router = axum::Router::new().route(
                "/creation/brainstorm/stage/batch",
                axum::routing::post(move |Json(body): Json<Value>| {
                    let count = observed.clone();
                    async move {
                        let attempt = count.fetch_add(1, Ordering::SeqCst);
                        if body["kind"] == "plan" {
                            return Json(compiled_plan_for_request(&body)).into_response();
                        }
                        if attempt == 1 {
                            match failure_kind {
                                "invalid_output" => {
                                    return (
                                        StatusCode::BAD_GATEWAY,
                                        Json(json!({"detail":{
                                    "code":"BRAINSTORM_MODEL_OUTPUT_INVALID","retryable":false}})),
                                    )
                                        .into_response()
                                }
                                "legacy_questions_response" => {
                                    return Json(json!({"questions":[question("root",None)]}))
                                        .into_response()
                                }
                                "permission" => {
                                    return (
                                        StatusCode::FORBIDDEN,
                                        Json(json!({"detail":{
                                    "code":"MODEL_ACCESS_DENIED","retryable":true}})),
                                    )
                                        .into_response()
                                }
                                _ => {
                                    return (
                                        StatusCode::UNPROCESSABLE_ENTITY,
                                        Json(json!({"detail":"invalid request"})),
                                    )
                                        .into_response()
                                }
                            }
                        }
                        Json(http_preparation_output(&body)).into_response()
                    }
                }),
            );
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let url = format!("http://{}", listener.local_addr().unwrap());
            let server = tokio::spawn(async move {
                axum::serve(listener, router).await.unwrap();
            });
            let legacy = initial();
            let (_tmp, state) = fixture(Some(&legacy), &url);
            act(&state, &request("start", 0)).await.unwrap();
            tokio::time::timeout(Duration::from_secs(3), async {
                loop {
                    if decode(&load(&state))
                        .unwrap()
                        .stage_workflow
                        .unwrap()
                        .current()
                        .status
                        == "failed"
                    {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .unwrap();
            let can_retry = matches!(failure_kind, "invalid_output" | "legacy_questions_response");
            let session = load(&state);
            let persisted: BrainstormStoredState =
                serde_json::from_str(&session.state_json).unwrap();
            let stage = persisted.stage_workflow.as_ref().unwrap().current();
            assert_eq!(stage.error.as_ref().unwrap()["retryable"], can_retry);
            assert_eq!(stage.progress.completed_batches, 1);
            if can_retry {
                // Reproduce an old persisted failure whose legacy error said
                // false: compatibility reads fix the button without inference.
                let mut old = persisted;
                old.stage_workflow
                    .as_mut()
                    .unwrap()
                    .current_mut()
                    .error
                    .as_mut()
                    .unwrap()["retryable"] = json!(false);
                persist(&state, SESSION, session.revision, "preparing", &old).unwrap();
            }
            let status = act(&state, &request("status", 0)).await.unwrap();
            assert_eq!(
                status
                    .stage_workflow
                    .unwrap()
                    .current()
                    .error
                    .as_ref()
                    .unwrap()["retryable"],
                can_retry
            );
            assert_eq!(count.load(Ordering::SeqCst), 2);
            if can_retry {
                act(&state, &request("retry_stage", status.revision))
                    .await
                    .unwrap();
                tokio::time::timeout(Duration::from_secs(3), async {
                    loop {
                        if load(&state).phase == "exploring" {
                            break;
                        }
                        tokio::time::sleep(Duration::from_millis(10)).await;
                    }
                })
                .await
                .unwrap();
                assert_eq!(count.load(Ordering::SeqCst), 4);
                assert_eq!(
                    decode(&load(&state))
                        .unwrap()
                        .stage_workflow
                        .unwrap()
                        .current()
                        .progress
                        .completed_batches,
                    2
                );
            }
            server.abort();
        }
    }

    #[test]
    fn full_tree_requires_every_option_and_rewrites_parent_ids() {
        let q = normalized_questions(tree(), "g", 1).unwrap();
        assert_eq!(q[1].parent_question_id.as_ref(), Some(&q[0].id));
        assert_ne!(q[0].id, "root");
        let mut incomplete = tree();
        incomplete["questions"].as_array_mut().unwrap().pop();
        assert!(normalized_questions(incomplete, "g", 1).is_err());
        let mut cycle = tree();
        cycle["questions"][0]["parent_question_id"] = json!("left");
        cycle["questions"][0]["parent_option_id"] = json!("a");
        assert!(normalized_questions(cycle, "g", 1).is_err());
    }

    #[test]
    fn duplicate_prompt_fingerprints_reject_batch_before_any_saved_state_changes() {
        for duplicate_kind in ["within_batch", "previous_topic"] {
            let mut stored = initial();
            apply_batch(
                &mut stored,
                json!({"topics":[
                {"id":"one","title":"主题一","goal":"讨论一"},
                {"id":"two","title":"主题二","goal":"讨论二"}]}),
            )
            .unwrap();
            apply_batch(&mut stored, four_question_tree()).unwrap();
            stored.memory_brief = "已保存来源说明".into();
            let before = serde_json::to_value(&stored).unwrap();
            let mut candidate = tree_for_topic("主题二");
            let repeated = if duplicate_kind == "within_batch" {
                candidate["questions"][1]["prompt"]
                    .as_str()
                    .unwrap()
                    .to_string()
            } else {
                stored.stage_workflow.as_ref().unwrap().current().questions[0]
                    .prompt
                    .clone()
            };
            candidate["questions"][2]["prompt"] = json!(format!(" {} !!!", repeated));
            candidate["memory_brief"] = json!("不得写入的失败候选来源");
            let failure = apply_batch(&mut stored, candidate).unwrap_err();
            assert_eq!(failure.1 .0["code"], "BRAINSTORM_MODEL_OUTPUT_INVALID");
            assert_eq!(serde_json::to_value(&stored).unwrap(), before);
        }
    }

    #[test]
    fn navigation_visits_all_topic_roots_before_selected_children() {
        let mut stored = initial();
        apply_batch(&mut stored,json!({"topics":[{"id":"one","title":"主题一","goal":"讨论一"},{"id":"two","title":"主题二","goal":"讨论二"}]})).unwrap();
        apply_batch(&mut stored, tree()).unwrap();
        apply_batch(&mut stored, tree_for_topic("主题二")).unwrap();
        let first = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: first,
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: String::new(),
                source: "user".into(),
            },
        });
        let second = next_question(&stored).unwrap();
        assert!(second.parent_question_id.is_none());
        stored.turns.push(BrainstormStoredTurn {
            question: second,
            answer: BrainstormAnswer {
                selected_option_ids: vec!["b".into()],
                custom_text: String::new(),
                source: "user".into(),
            },
        });
        assert_eq!(
            next_question(&stored).unwrap().parent_option_id.as_deref(),
            Some("a")
        );
    }

    #[tokio::test]
    async fn answers_and_revisions_traverse_saved_choices_without_sidecar() {
        let stored = ready();
        let root = stored.current_question.clone().unwrap();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let answer = act(&state, &selected(&root, "a", 0)).await.unwrap();
        let left = answer.current_question.unwrap();
        assert_eq!(left.parent_option_id.as_deref(), Some("a"));
        let answer = act(&state, &selected(&left, "a", 1)).await.unwrap();
        assert_eq!(answer.phase, "ready");
        assert_eq!(answer.answered_count, 2);
        let mut req = selected(&root, "b", 2);
        req.action = "revise_answer".into();
        let answer = act(&state, &req).await.unwrap();
        assert_eq!(
            answer.current_question.unwrap().parent_option_id.as_deref(),
            Some("b")
        );
        assert_eq!(answer.answered_count, 1);
        assert!(answer
            .archived_questions
            .iter()
            .any(|q| q.question.id == left.id));
        assert!(state.brainstorm_stage_jobs.lock().unwrap().is_empty());
    }

    #[test]
    fn skipped_or_assumed_receipts_never_become_confirmed_in_the_brief() {
        for source in ["user_skipped", "agent_assumption"] {
            let mut stored = ready();
            let question = stored.current_question.take().unwrap();
            stored.turns.push(BrainstormStoredTurn {
                question,
                answer: BrainstormAnswer {
                    selected_option_ids: vec![],
                    custom_text: "暂未确定，保留待核验".into(),
                    source: source.into(),
                },
            });
            let brief = brainstorm_brief(&stored);
            assert!(!brief.contains("**已确认：**"));
            let context = brainstorm_decision_context(&stored);
            assert_eq!(context[0]["answer_source"], source);
            assert!(context[0]["user_inputs"].as_array().unwrap().is_empty());
            assert!(context[0]["selected_options"]
                .as_array()
                .unwrap()
                .is_empty());
        }
    }

    #[tokio::test]
    async fn custom_skip_exclude_do_not_activate_unselected_descendants() {
        for action in ["answer", "skip", "exclude"] {
            let stored = ready();
            let root = stored.current_question.clone().unwrap();
            let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
            let mut req = request(action, 0);
            req.question_id = Some(root.id);
            if action == "answer" {
                req.answer = Some(BrainstormAnswer {
                    selected_option_ids: vec![],
                    custom_text: "采用全新混合方向".into(),
                    source: "user".into(),
                });
            }
            let response = act(&state, &req).await.unwrap();
            assert_eq!(response.phase, "ready");
            assert_eq!(response.answered_count, 1);
            assert_eq!(
                response.stage_workflow.unwrap().current().questions.len(),
                3
            );
        }
    }

    #[tokio::test]
    async fn dropping_sse_body_after_headers_keeps_start_and_stage_confirmation_committed() {
        let (_tmp, state) = fixture(None, "http://127.0.0.1:9");
        let mut headers = HeaderMap::new();
        headers.insert(header::ACCEPT, "text/event-stream".parse().unwrap());
        let response = run_creation_brainstorm_turn(
            State(state.clone()),
            headers.clone(),
            Json(request("start", 0)),
        )
        .await;
        drop(response); // No body frame has ever been polled.
        assert!(decode(&load(&state)).unwrap().stage_workflow.is_some());
        let mut stored = ready();
        let root = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: root,
            answer: BrainstormAnswer {
                selected_option_ids: vec![],
                custom_text: "另一个已确认方向".into(),
                source: "user".into(),
            },
        });
        select_next(&mut stored);
        let (_tmp2, state2) = fixture(Some(&stored), "http://127.0.0.1:9");
        let mut req = request("confirm_stage", 0);
        req.workflow_version = None;
        let response =
            run_creation_brainstorm_turn(State(state2.clone()), headers, Json(req)).await;
        drop(response);
        assert_eq!(
            decode(&load(&state2))
                .unwrap()
                .stage_workflow
                .unwrap()
                .active_stage,
            "detail"
        );
    }

    #[tokio::test]
    async fn abandon_is_idempotent_and_blocks_late_stage_results() {
        let stored = ready();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let response = act(&state, &request("abandon", 0)).await.unwrap();
        assert!(response.current_question.is_none());
        let again = act(&state, &request("abandon", 0)).await.unwrap();
        assert_eq!(again.revision, response.revision);
        assert!(persist(&state, SESSION, 0, "exploring", &stored).is_err());
        assert!(act(&state, &request("status", 1)).await.is_err());
    }

    #[test]
    fn changing_upstream_archives_generated_future_tree_and_preserves_current_tree() {
        let mut stored = ready();
        let root = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: root.clone(),
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: String::new(),
                source: "user".into(),
            },
        });
        let w = stored.stage_workflow.as_mut().unwrap();
        w.stages[0].status = "completed".into();
        w.active_stage = "detail".into();
        w.stages[1].status = "thinking".into();
        apply_batch(&mut stored, plan()).unwrap();
        apply_batch(&mut stored, tree()).unwrap();
        let previous = stored.stage_workflow.as_ref().unwrap().stages[1]
            .generation_id
            .clone();
        let mut req = selected(&root, "b", 5);
        req.action = "revise_answer".into();
        revise_saved_answer(&mut stored, &req, 6).unwrap();
        let w = stored.stage_workflow.as_ref().unwrap();
        assert_eq!(w.active_stage, "direction");
        assert_eq!(w.stages[0].questions.len(), 3);
        assert_eq!(w.stages[1].status, "pending");
        assert!(w.stages[1].questions.is_empty());
        assert_ne!(w.stages[1].generation_id, previous);
        assert_eq!(stored.archived_questions.len(), 4);
    }

    #[test]
    fn editable_brief_replans_root_and_archives_answers_without_erasing_text() {
        let mut stored = ready();
        let root = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: root.clone(),
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: String::new(),
                source: "user".into(),
            },
        });
        let mut req = request("edit_brief", 0);
        req.brief_edits
            .insert(root.id.clone(), "本次仅讨论组织方式".into());
        assert_eq!(edit_saved_brief(&mut stored, &req, 1).unwrap(), "ready");
        assert_eq!(stored.turns[0].answer.custom_text, "本次仅讨论组织方式");
        req.brief_edits.clear();
        req.brief_edits
            .insert("root_request".into(), "重新规划培训方案".into());
        assert_eq!(edit_saved_brief(&mut stored, &req, 2).unwrap(), "preparing");
        assert!(stored.turns.is_empty());
        assert_eq!(stored.root_request, "重新规划培训方案");
        assert!(stored.archived_questions.iter().any(|q| q
            .answer
            .as_ref()
            .is_some_and(|a| a.custom_text == "本次仅讨论组织方式")));
        assert_eq!(
            stored
                .stage_workflow
                .unwrap()
                .current()
                .progress
                .completed_batches,
            0
        );
    }

    #[test]
    fn editable_brief_root_replan_retains_copy_repair_audit_for_archived_questions() {
        let mut stored = ready();
        let question = stored.current_question.as_ref().unwrap().clone();
        let old_generation = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .generation_id
            .clone();
        let mut repair = request("repair_candidate_copy", 0);
        repair.candidate_copy_patches = vec![why_patch(
            &question,
            "候选组织方式尚未确认，先比较各自的适用条件",
        )];
        repair_candidate_copy(&mut stored, &repair, 1).unwrap();
        let history =
            serde_json::to_value(&stored.stage_workflow.as_ref().unwrap().copy_repair_history)
                .unwrap();

        let mut edit = request("edit_brief", 1);
        edit.brief_edits
            .insert("root_request".into(), "重新规划培训方案".into());
        assert_eq!(
            edit_saved_brief(&mut stored, &edit, 2).unwrap(),
            "preparing"
        );

        let workflow = stored.stage_workflow.as_ref().unwrap();
        assert_eq!(
            serde_json::to_value(&workflow.copy_repair_history).unwrap(),
            history
        );
        assert_ne!(workflow.current().generation_id, old_generation);
        assert!(workflow.current().questions.is_empty());
        assert_eq!(workflow.current().progress.completed_batches, 0);
        let archived = stored
            .archived_questions
            .iter()
            .find(|item| item.question.id == question.id)
            .unwrap();
        assert_eq!(
            archived.question.why_now,
            repair.candidate_copy_patches[0].text
        );
        assert!(archived.answer.is_none());

        // The evidence is durable even when a later root edit resets another plan.
        apply_batch(&mut stored, plan()).unwrap();
        apply_batch(&mut stored, tree()).unwrap();
        edit.brief_edits
            .insert("root_request".into(), "再次调整培训需求".into());
        edit_saved_brief(&mut stored, &edit, 3).unwrap();
        assert_eq!(
            serde_json::to_value(&stored.stage_workflow.as_ref().unwrap().copy_repair_history)
                .unwrap(),
            history
        );
    }

    #[test]
    fn clearing_brief_decision_does_not_reactivate_old_selected_branch() {
        let mut stored = ready();
        let root = stored.current_question.take().unwrap();
        stored.turns.push(BrainstormStoredTurn {
            question: root.clone(),
            answer: BrainstormAnswer {
                selected_option_ids: vec!["a".into()],
                custom_text: String::new(),
                source: "user".into(),
            },
        });
        select_next(&mut stored);
        assert!(stored.current_question.is_some());
        let mut req = request("edit_brief", 1);
        req.brief_edits.insert(root.id.clone(), String::new());
        assert_eq!(edit_saved_brief(&mut stored, &req, 2).unwrap(), "ready");
        assert!(stored.current_question.is_none());
        assert!(stored.turns[0].answer.selected_option_ids.is_empty());
        assert_eq!(stored.turns[0].answer.source, "user_cleared");
        assert_eq!(stored.brief_edits[&root.id], "");
        assert!(brainstorm_response(SESSION, "ready", 2, &stored)
            .decisions
            .is_empty());
        assert!(brainstorm_decision_context(&stored).is_empty());
    }

    #[test]
    fn chapter_skill_binding_keeps_frozen_roles_and_sources() {
        let mut stored = initial();
        stored.root_request = "请生成GPU成本优化的周报".into();
        stored.selected_skills = vec![
            json!({"id":"gpu","title":"GPU成本优化周报模板","workflowRole":"primary",
                "skillDescription":{"purpose":"GPU成本周报"},"executionSteps":[{"id":"collect","title":"取数"}]}),
            json!({"id":"evidence","title":"数据证据","workflowRole":"support",
                "skillDescription":{"purpose":"标注来源"},"executionSteps":[]}),
        ];
        let mut req: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"撰写第 1 章《创作正文》",
            "available_skills":[
                {"id":"unrelated","title":"其他模板"},
                {"id":"gpu","title":"GPU成本优化周报模板","workflowRole":"primary",
                    "skillDescription":{"purpose":"GPU成本周报"},"executionSteps":[{"id":"collect","title":"取数"}],
                    "skillInstructions":"按周报规则写作"},
                {"id":"evidence","title":"数据证据","workflowRole":"primary",
                    "skillDescription":{"purpose":"标注来源"},"executionSteps":[],
                    "skillInstructions":"只提供证据约束"}
            ]
        })).unwrap();
        bind_chapter_skills(&stored, &mut req).unwrap();
        let binding = req.brainstorm_skill_binding.as_ref().unwrap();
        assert_eq!(binding["version"], 1);
        assert_eq!(binding["skills"][0]["selection_source"], "automatic");
        assert_eq!(binding["skills"][0]["role"], "primary");
        assert_eq!(binding["skills"][1]["selection_source"], "dependency");
        assert_eq!(binding["skills"][1]["role"], "support");
        assert!(req.explicit_skill_ids.is_empty());
        assert_eq!(req.available_skills.as_ref().unwrap().len(), 2);
        assert_eq!(req.selected_skills[1]["workflowRole"], "support");
        assert_eq!(
            req.selected_skills[0]["skillInstructions"],
            "按周报规则写作"
        );

        let fingerprint = binding["fingerprint"].as_str().unwrap().to_string();
        let mut same: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"本章重试", "available_skills": req.available_skills
        }))
        .unwrap();
        bind_chapter_skills(&stored, &mut same).unwrap();
        assert_eq!(
            same.brainstorm_skill_binding.unwrap()["fingerprint"],
            fingerprint
        );
        let mut changed_executable: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"本章重试", "available_skills": same.available_skills
        }))
        .unwrap();
        changed_executable.available_skills.as_mut().unwrap()[0]["skillInstructions"] =
            json!("改过的周报规则");
        bind_chapter_skills(&stored, &mut changed_executable).unwrap();
        assert_ne!(
            changed_executable.brainstorm_skill_binding.unwrap()["fingerprint"],
            fingerprint
        );
    }

    #[test]
    fn chapter_skill_binding_only_infers_explicit_from_original_literal_mention() {
        let mut stored = initial();
        stored.selected_skills = vec![json!({"id":"gpu","title":"GPU成本优化周报模板",
            "workflowRole":"primary","executionSteps":[{"id":"write"}]})];
        let candidate = json!({"id":"gpu","title":"GPU成本优化周报模板",
            "workflowRole":"primary","executionSteps":[{"id":"write"}]});
        stored.root_request = "请参考 GPU成本优化周报模板 生成周报".into();
        let mut automatic: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"章节写作", "available_skills":[candidate.clone()]
        }))
        .unwrap();
        bind_chapter_skills(&stored, &mut automatic).unwrap();
        assert!(automatic.explicit_skill_ids.is_empty());
        stored.root_request = "请用 @ GPU成本优化周报模板 生成周报".into();
        let mut spaced: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"章节写作", "available_skills":[candidate.clone()]
        }))
        .unwrap();
        bind_chapter_skills(&stored, &mut spaced).unwrap();
        assert!(spaced.explicit_skill_ids.is_empty());
        stored.root_request = "请用 @GPU成本优化周报模板 生成周报".into();
        let mut explicit: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"章节写作", "available_skills":[candidate.clone()]
        }))
        .unwrap();
        bind_chapter_skills(&stored, &mut explicit).unwrap();
        assert_eq!(explicit.explicit_skill_ids, ["gpu"]);
        assert_eq!(
            explicit.brainstorm_skill_binding.unwrap()["skills"][0]["selection_source"],
            "user"
        );

        stored.selected_skills[0]["selectionSource"] = json!("user");
        stored.root_request = "请参考 GPU成本优化周报模板".into();
        let mut forged: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"章节写作", "available_skills":[candidate]
        }))
        .unwrap();
        assert_eq!(
            bind_chapter_skills(&stored, &mut forged).unwrap_err().1,
            "BRAINSTORM_SKILL_BINDING_UNAVAILABLE"
        );
    }

    #[test]
    fn chapter_skill_binding_rejects_missing_or_changed_executable_profile() {
        let mut stored = initial();
        stored.selected_skills = vec![json!({"id":"gpu","title":"GPU周报模板",
            "workflowRole":"primary","selectionSource":"automatic",
            "skillDescription":{"purpose":"每周成本分析"},
            "executionSteps":[{"id":"collect","tools":["data_search"]}]})];
        let mut missing: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"章节写作", "available_skills":[]
        }))
        .unwrap();
        assert_eq!(
            bind_chapter_skills(&stored, &mut missing).unwrap_err().1,
            "BRAINSTORM_SKILL_BINDING_UNAVAILABLE"
        );
        let mut changed: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"章节写作", "available_skills":[{"id":"gpu","title":"GPU周报模板",
                "skillDescription":{"purpose":"每周成本分析"},
                "executionSteps":[{"id":"collect","tools":[]}] }]
        }))
        .unwrap();
        assert_eq!(
            bind_chapter_skills(&stored, &mut changed).unwrap_err().1,
            "BRAINSTORM_SKILL_BINDING_UNAVAILABLE"
        );
    }

    #[test]
    fn chapter_request_uses_canonical_saved_skill_instead_of_client_catalog() {
        let mut stored = initial();
        stored.root_request = "请生成GPU成本优化的周报".into();
        stored.selected_skills = vec![json!({"id":"gpu","title":"GPU成本优化周报模板",
            "workflowRole":"primary","skillDescription":{"purpose":"GPU成本周报"},
            "executionSteps":[{"id":"collect","title":"取数"}]})];
        let workflow = stored.stage_workflow.as_mut().unwrap();
        workflow.active_stage = "writing".into();
        workflow.active_chapter_id = Some("chapter-one".into());
        workflow.chapters = vec![Chapter {
            id: "chapter-one".into(),
            title: "创作正文".into(),
            goal: "完成周报正文".into(),
            status: "writing".into(),
            question_ids: Vec::new(),
            instruction_id: Some("instruction-one".into()),
            operation_id: Some("operation-one".into()),
        }];
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let mut req: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"客户端另造的标题", "session_id":SESSION,
            "brainstorm_chapter_id":"chapter-one", "instruction_id":"instruction-one",
            "available_skills":[
                {"id":"other","title":"别的技能"},
                {"id":"gpu","title":"GPU成本优化周报模板","workflowRole":"primary",
                    "skillDescription":{"purpose":"GPU成本周报"},
                    "executionSteps":[{"id":"collect","title":"取数"}],
                    "skillInstructions":"按 GPU 周报政策写作"}
            ]
        }))
        .unwrap();
        bind_chapter_request(&state, &mut req).unwrap();
        assert_eq!(req.root_request.as_deref(), Some("请生成GPU成本优化的周报"));
        assert_eq!(req.available_skills.as_ref().unwrap().len(), 1);
        assert_eq!(req.available_skills.as_ref().unwrap()[0]["id"], "gpu");
        assert_eq!(
            req.brainstorm_skill_binding.as_ref().unwrap()["skills"][0]["selection_source"],
            "automatic"
        );
        assert!(req.explicit_skill_ids.is_empty());
    }

    #[test]
    fn chapter_request_binds_period_only_after_saved_user_answer() {
        let mut stored = initial();
        stored.root_request = "请生成 GPU 周报".into();
        stored.selected_skills = vec![json!({"id":"gpu","title":"GPU周报模板",
            "workflowRole":"primary","skillDescription":{"purpose":"GPU周报"},
            "executionSteps":[{"id":"collect","title":"取数"}]})];
        let period_question: BrainstormQuestion = serde_json::from_value(json!({
            "id":"input_report_period_case","dimension":"报告周期",
            "type":"single_choice","prompt":"这份周报覆盖哪一周？",
            "why_now":"先确认范围","required":true,"allow_custom":true,
            "options":[{"id":"previous","label":"最近完整一周",
                "description":"2026-09-21 至 2026-09-27","recommended":false,
                "input_value":{"start":"2026-09-21","end":"2026-09-27",
                    "timezone":"Asia/Shanghai"}}],
            "input_clarification":{"kind":"report_period","need_ids":[]}
        })).unwrap();
        let workflow = stored.stage_workflow.as_mut().unwrap();
        let detail = workflow.stages.iter_mut().find(|stage| stage.id == "detail").unwrap();
        detail.questions.push(period_question.clone());
        detail.input_phase = "complete".into();
        detail.input_preflight = Some(json!({"schema_version":1,"status":"matched",
            "timezone":"Asia/Shanghai","period":{"status":"confirmed",
                "start":"2026-09-21","end":"2026-09-27"},"needs":[]}));
        detail.confirmed_period = Some(json!({"start":"2026-09-21","end":"2026-09-27",
            "timezone":"Asia/Shanghai","source":"user"}));
        workflow.active_stage = "writing".into();
        workflow.active_chapter_id = Some("chapter-one".into());
        workflow.chapters = vec![Chapter {
            id: "chapter-one".into(), title: "正文".into(), goal: "完成周报".into(),
            status: "writing".into(), question_ids: Vec::new(),
            instruction_id: Some("instruction-one".into()),
            operation_id: Some("operation-one".into()),
        }];
        let request = || serde_json::from_value::<AgentRunRequest>(json!({
            "user_prompt":"章节写作", "session_id":SESSION,
            "brainstorm_chapter_id":"chapter-one", "instruction_id":"instruction-one",
            "available_skills":[{"id":"gpu","title":"GPU周报模板",
                "skillDescription":{"purpose":"GPU周报"},
                "executionSteps":[{"id":"collect","title":"取数"}]}]
        })).unwrap();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let mut unsigned = request();
        bind_chapter_request(&state, &mut unsigned).unwrap();
        assert!(unsigned.creation_brief.unwrap().get("confirmed_report_period").is_none());
        let unsigned_json = serde_json::to_string(&stored).unwrap();
        state.storage.with_conn(|conn| {
            assert!(repo::update(conn, SESSION, 0, "completed", &unsigned_json)?);
            Ok(())
        }).unwrap();
        let mut unsigned_edit: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"修改已完成周报", "session_id":SESSION
        })).unwrap();
        bind_chapter_request(&state, &mut unsigned_edit).unwrap();
        assert!(unsigned_edit.creation_brief.unwrap().get("confirmed_report_period").is_none());

        stored.turns.push(BrainstormStoredTurn {
            question: period_question, answer: BrainstormAnswer {
                selected_option_ids: vec!["previous".into()],
                custom_text: String::new(), source: "user".into(),
            },
        });
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let mut signed = request();
        bind_chapter_request(&state, &mut signed).unwrap();
        assert_eq!(signed.creation_brief.unwrap()["confirmed_report_period"], json!({
            "start":"2026-09-21","end":"2026-09-27",
            "timezone":"Asia/Shanghai","source":"user"
        }));
        let signed_json = serde_json::to_string(&stored).unwrap();
        state.storage.with_conn(|conn| {
            assert!(repo::update(conn, SESSION, 0, "completed", &signed_json)?);
            Ok(())
        }).unwrap();
        let mut signed_edit: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"修改已完成周报", "session_id":SESSION
        })).unwrap();
        bind_chapter_request(&state, &mut signed_edit).unwrap();
        assert_eq!(signed_edit.creation_brief.unwrap()["confirmed_report_period"], json!({
            "start":"2026-09-21","end":"2026-09-27",
            "timezone":"Asia/Shanghai","source":"user"
        }));
    }

    #[test]
    fn v17_chapter_passes_minimal_source_gap_contract_without_preflight_receipts() {
        let mut stored = initial();
        stored.root_request = "请生成 GPU 成本周报".into();
        stored.selected_skills.clear();
        let period_question: BrainstormQuestion = serde_json::from_value(json!({
            "id":"period", "dimension":"报告周期", "type":"single_choice",
            "prompt":"这份周报覆盖哪一周？", "why_now":"先确认范围",
            "required":true, "allow_custom":true,
            "options":[{"id":"previous", "label":"最近完整一周",
                "description":"2026-09-21 至 2026-09-27", "recommended":false,
                "input_value":{"start":"2026-09-21","end":"2026-09-27",
                    "timezone":"Asia/Shanghai"}}],
            "input_clarification":{"kind":"report_period","need_ids":["need-bill"]}
        })).unwrap();
        let source_question: BrainstormQuestion = serde_json::from_value(json!({
            "id":"source", "dimension":"来源确认", "type":"single_choice",
            "prompt":"本期 GPU 账单来源是否可核对？", "why_now":"核对费用",
            "required":true, "allow_custom":true,
            "options":[{"id":"draft_with_gap", "label":"标明缺口先成稿",
                "description":"待核账单", "recommended":false,
                "input_value":{"action":"draft_with_gap","need_id":"need-bill",
                    "target_period":{"start":"2026-09-21","end":"2026-09-27",
                        "timezone":"Asia/Shanghai"}}}],
            "input_clarification":{"kind":"source_confirmation","need_ids":["need-bill"]}
        })).unwrap();
        let workflow = stored.stage_workflow.as_mut().unwrap();
        for stage in &mut workflow.stages {
            stage.preparation_contract_version = CONTENT_PREPARATION_CONTRACT_VERSION;
        }
        let detail = workflow.stages.iter_mut().find(|stage| stage.id == "detail").unwrap();
        detail.questions.extend([period_question.clone(), source_question.clone()]);
        detail.input_phase = "complete".into();
        detail.input_preflight = Some(json!({"status":"partial", "timezone":"Asia/Shanghai",
            "period":{"status":"confirmed","start":"2026-09-21","end":"2026-09-27",
                "source":"user"},
            "needs":[{"id":"need-bill","label":"GPU 账单费用可核验来源",
                "status":"candidate","receipts":[{"title":"内部敏感来源"}]}]}));
        detail.confirmed_period = Some(json!({"start":"2026-09-21","end":"2026-09-27",
            "timezone":"Asia/Shanghai","source":"user"}));
        workflow.active_stage = "writing".into();
        workflow.active_chapter_id = Some("body".into());
        workflow.chapters = vec![Chapter {id:"body".into(), title:"创作正文".into(),
            goal:"完成周报".into(), status:"writing".into(), question_ids:vec![],
            instruction_id:Some("instruction".into()), operation_id:Some("operation".into())}];
        stored.turns.push(BrainstormStoredTurn {question:period_question,
            answer:BrainstormAnswer {selected_option_ids:vec!["previous".into()],
                custom_text:String::new(), source:"user".into()}});
        stored.turns.push(BrainstormStoredTurn {question:source_question,
            answer:BrainstormAnswer {selected_option_ids:vec!["draft_with_gap".into()],
                custom_text:String::new(), source:"user".into()}});
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let mut req: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"章节写作", "session_id":SESSION,
            "brainstorm_chapter_id":"body", "instruction_id":"instruction",
            "available_skills":[{"id":"catalog-only","title":"未选目录项"}],
            "selected_skills":[{"id":"catalog-only","title":"未选目录项"}],
            "explicit_skill_ids":["catalog-only"]
        })).unwrap();
        bind_chapter_request(&state, &mut req).unwrap();
        assert!(req.available_skills.as_ref().unwrap().is_empty());
        assert!(req.selected_skills.is_empty());
        assert!(req.explicit_skill_ids.is_empty());
        let brief = req.creation_brief.unwrap();
        assert!(brief.get("stage_workflow").is_none());
        assert_eq!(brief["content_source_gap"]["schema_version"],
            "brainstorm.content-source-gap.v1");
        assert_eq!(brief["content_source_gap"]["input_preflight"]["needs"][0]["id"],
            "need-bill");
        assert!(brief["content_source_gap"].to_string().find("内部敏感来源").is_none());

        // An ordinary edit of a completed provisional report must receive the
        // same private constraint; the public session has no such field.
        assert!(serde_json::to_value(brainstorm_response(SESSION, "completed", 1, &stored))
            .unwrap().get("content_source_gap").is_none());
        let stored_json = serde_json::to_string(&stored).unwrap();
        state.storage.with_conn(|conn| {
            assert!(repo::update(conn, SESSION, 0, "completed", &stored_json)?);
            Ok(())
        }).unwrap();
        let mut followup: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"润色已完成周报", "session_id":SESSION
        })).unwrap();
        bind_chapter_request(&state, &mut followup).unwrap();
        let followup_gap = &followup.creation_brief.as_ref().unwrap()["content_source_gap"];
        assert_eq!(followup_gap["schema_version"], "brainstorm.content-source-gap.v1");
        assert!(followup_gap.to_string().find("内部敏感来源").is_none());

        // A later candidate click, even alongside plausible source-level
        // period metadata, does not verify the report's metric rows or values.
        let mut candidate_stored = stored.clone();
        let receipt = json!({"source_type":"data", "source_id":42,
            "source_snapshot_id":"snapshot", "source_period":{
                "start":"2026-09-21","end":"2026-09-27"},
            "period_status":"matched", "completeness":"complete", "can_use":true,
            "title":"内部敏感来源"});
        let detail = candidate_stored.stage_workflow.as_mut().unwrap().stages.iter_mut()
            .find(|stage| stage.id == "detail").unwrap();
        detail.input_preflight.as_mut().unwrap()["needs"][0] = json!({
            "id":"need-bill", "label":"GPU 账单费用可核验来源",
            "status":"matched", "receipts":[receipt]
        });
        let source_turn = candidate_stored.turns.last_mut().unwrap();
        source_turn.question.options.push(serde_json::from_value(json!({
            "id":"candidate_1", "label":"确认候选", "description":"确认目标周期",
            "input_value":{"action":"confirm_candidate", "need_id":"need-bill",
                "source_id":42, "receipt_fingerprint":"candidate-only",
                "target_period":{"start":"2026-09-21","end":"2026-09-27",
                    "timezone":"Asia/Shanghai"}}
        })).unwrap());
        source_turn.answer.selected_option_ids = vec!["candidate_1".into()];
        let (candidate_tmp, candidate_state) = fixture(Some(&candidate_stored), "http://127.0.0.1:9");
        let candidate_json = serde_json::to_string(&candidate_stored).unwrap();
        candidate_state.storage.with_conn(|conn| {
            assert!(repo::update(conn, SESSION, 0, "completed", &candidate_json)?);
            Ok(())
        }).unwrap();
        let mut candidate_followup: AgentRunRequest = serde_json::from_value(json!({
            "user_prompt":"根据候选资料修订周报", "session_id":SESSION
        })).unwrap();
        bind_chapter_request(&candidate_state, &mut candidate_followup).unwrap();
        assert_eq!(candidate_followup.creation_brief.unwrap()["content_source_gap"]["schema_version"],
            "brainstorm.content-source-gap.v1");
        drop(candidate_tmp);
    }

    #[tokio::test]
    async fn chapter_completion_requires_matching_durable_review_and_committed_document() {
        let mut stored = initial();
        stored.stage_workflow.as_mut().unwrap().active_stage = "writing".into();
        apply_batch(&mut stored,json!({"topics":[{"id":"a","title":"开篇","goal":"开篇设计","chapter_id":"one"},
            {"id":"b","title":"结尾","goal":"结尾设计","chapter_id":"two"}],
            "chapters":[{"id":"one","title":"开篇","goal":"说明目标"},{"id":"two","title":"结尾","goal":"说明行动"}]})).unwrap();
        apply_batch(&mut stored, tree()).unwrap();
        apply_batch(&mut stored, tree_for_topic("结尾章节")).unwrap();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let response = act(
            &state,
            &selected(stored.current_question.as_ref().unwrap(), "a", 0),
        )
        .await
        .unwrap();
        let response = act(
            &state,
            &selected(response.current_question.as_ref().unwrap(), "a", 1),
        )
        .await
        .unwrap();
        assert_eq!(response.phase, "ready");
        let mut confirm = request("confirm_chapter", 2);
        confirm.chapter_id = Some("one".into());
        let response = act(&state, &confirm).await.unwrap();
        let chapter = response.stage_workflow.unwrap().chapters[0].clone();
        let mut completed = request("chapter_completed", 3);
        completed.chapter_id = Some("one".into());
        assert!(act(&state, &completed).await.is_err());
        let mut run: AgentRunRequest =
            serde_json::from_value(json!({"user_prompt":"伪造的全篇重写", "session_id":SESSION,
            "brainstorm_chapter_id":"one", "instruction_id":chapter.instruction_id}))
            .unwrap();
        bind_chapter_request(&state, &mut run).unwrap();
        assert!(run.generation.user_prompt.starts_with("撰写第 1 章"));
        let brief = run.creation_brief.unwrap();
        assert!(brief.get("stage_workflow").is_none());
        assert_eq!(brief["chapter_context"]["index"], 1);
        assert_eq!(
            brief["chapter_context"]["confirmed_decisions"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        let operation_id = chapter.operation_id.as_deref().unwrap();
        state.storage.with_conn(|conn| {
            let history_id=creation_history::insert(conn,"根需求","",None,None,0,None,None,None,Some(SESSION),None,None,None,Some("根需求"),None,1,"initial",None)?;
            let history=creation_history::get_by_id(conn,history_id)?.unwrap();
            creation_operation::begin(conn,SESSION,operation_id,chapter.instruction_id.as_deref().unwrap(),"本章写作",&history)?;
            let mut review=json!({"type":"delivery.checked","run_id":"chapter-run","event_id":"review","sequence":1,"data":{"review":{"status":"pass"}}});
            creation_operation::record_event(conn,SESSION,operation_id,&mut review)?;
            let mut event=json!({"type":"run.completed","status":"completed","run_id":"chapter-run","event_id":"completed","sequence":2,
                "data":{"document":"## 开篇\n本章内容","delivery_review":{"status":"pass"},"goal":{"status":"complete"}}});
            creation_operation::record_event(conn,SESSION,operation_id,&mut event)?; Ok(())
        }).unwrap();
        let mut retry: AgentRunRequest =
            serde_json::from_value(json!({"user_prompt":"新的客户端包装", "session_id":SESSION,
            "brainstorm_chapter_id":"one", "instruction_id":chapter.instruction_id}))
            .unwrap();
        bind_chapter_request(&state, &mut retry).unwrap();
        assert_eq!(
            retry.generation.user_prompt, "本章写作",
            "retry must keep the exact persisted instruction"
        );
        let response = act(&state, &completed).await.unwrap();
        let w = response.stage_workflow.unwrap();
        assert_eq!(w.chapters[0].status, "completed");
        assert_eq!(w.active_chapter_id.as_deref(), Some("two"));
        assert_eq!(w.chapters[1].status, "answering");
        let mut stale = decode(&load(&state)).unwrap();
        let mut revise = request("reopen", 4);
        revise.question_id = Some(stored.current_question.unwrap().id);
        assert_eq!(
            revise_saved_answer(&mut stale, &revise, 5)
                .unwrap_err()
                .1
                 .0["code"],
            "BRAINSTORM_CHAPTER_REVIEW_REQUIRED"
        );
    }

    #[tokio::test]
    async fn stale_status_snapshot_cannot_abort_new_stage_worker() {
        let old = ready();
        let (_tmp, state) = fixture(Some(&old), "http://127.0.0.1:9");
        let old_session = load(&state);
        let mut current = old.clone();
        let w = current.stage_workflow.as_mut().unwrap();
        w.active_stage = "detail".into();
        w.current_mut().status = "thinking".into();
        let key = format!("{}:{}", SESSION, w.current().generation_id);
        persist(&state, SESSION, 0, "preparing", &current).unwrap();
        state
            .brainstorm_stage_jobs
            .lock()
            .unwrap()
            .insert(key.clone(), tokio::spawn(std::future::pending()));
        schedule(&state, &request("status", 0), &old_session, &old);
        let mut jobs = state.brainstorm_stage_jobs.lock().unwrap();
        assert!(
            jobs.contains_key(&key),
            "a stale status must leave the active-stage worker registered"
        );
        let worker = jobs.remove(&key).unwrap();
        assert!(!worker.is_finished());
        worker.abort();
    }

    #[test]
    fn completed_workflow_filters_only_precise_legacy_root_seed_without_mutating_history() {
        let stored = ready();
        let (_tmp, state) = fixture(Some(&stored), "http://127.0.0.1:9");
        let mut req: AgentRunRequest =
            serde_json::from_value(json!({"user_prompt":"修改语气","session_id":SESSION})).unwrap();
        let root = stored.root_request.clone();
        let pending = vec![
            json!({"operation_id":format!("legacy-operation-{}-root",SESSION),"instruction":root}),
            json!({"operation_id":"operation-real-edit","instruction":root}),
            json!({"operation_id":format!("legacy-operation-{}-real-edit",SESSION),"instruction":"补充真正未完成的表格"}),
            json!({"operation_id":"legacy-operation-other-session-root","instruction":root}),
        ];
        let before = load(&state).state_json;
        let filtered = scope_pending_operations(&state, &req, pending.clone()).unwrap();
        assert_eq!(filtered.len(), 3);
        assert!(filtered
            .iter()
            .any(|p| p["operation_id"] == "operation-real-edit"));
        req.brainstorm_chapter_id = Some("chapter".into());
        assert!(scope_pending_operations(&state, &req, pending)
            .unwrap()
            .is_empty());
        assert_eq!(
            load(&state).state_json,
            before,
            "context projection must not alter durable records"
        );
    }

    #[tokio::test]
    async fn chapter_agent_entry_does_not_seed_original_full_article_as_another_task() {
        let (tx, mut rx) = tokio::sync::mpsc::channel(1);
        let router=axum::Router::new().route("/creation/agent/run",axum::routing::post(move |Json(payload):Json<Value>| {
            let tx=tx.clone(); async move {
                tx.send(payload.clone()).await.unwrap();
                format!("data: {}\n\n",json!({"type":"run.paused","event_id":"chapter-pause","sequence":1,
                    "session_id":payload["session_id"],"run_id":payload["run_id"],"data":{"reason":"test"}}))
            }
        }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let mut stored = initial();
        let w = stored.stage_workflow.as_mut().unwrap();
        w.active_stage = "writing".into();
        w.current_mut().status = "ready".into();
        w.active_chapter_id = Some("one".into());
        let operation_id = format!(
            "operation-{}",
            sha256_hex(&format!("{}:chapter-one", SESSION))
        );
        w.chapters = vec![Chapter {
            id: "one".into(),
            title: "开篇".into(),
            goal: "阐明目标".into(),
            status: "writing".into(),
            question_ids: vec![],
            instruction_id: Some("chapter-one".into()),
            operation_id: Some(operation_id.clone()),
        }];
        let (_tmp, state) = fixture(Some(&stored), &url);
        state.storage.with_conn(|conn| {
            creation_history::insert(conn,&stored.root_request,"",None,None,0,None,None,None,Some(SESSION),
                Some(&json!([{"id":"original-root","role":"user","content":stored.root_request}]).to_string()),
                Some("[]"),None,Some(&stored.root_request),None,1,"initial",None)?; Ok(())
        }).unwrap();
        let req: AgentRunRequest =
            serde_json::from_value(json!({"user_prompt":"章节创作","session_id":SESSION,
            "instruction_id":"chapter-one","brainstorm_chapter_id":"one","enable_rag":false}))
            .unwrap();
        let response = run_creation_agent(State(state.clone()), Json(req))
            .await
            .unwrap()
            .into_response();
        let body = axum::body::to_bytes(response.into_body(), 1024 * 1024)
            .await
            .unwrap();
        assert!(String::from_utf8_lossy(&body).contains("run.paused"));
        let forwarded = rx.recv().await.unwrap();
        assert!(forwarded["operation_context"]["pending_operations"]
            .as_array()
            .unwrap()
            .is_empty());
        let ids: Vec<String> = state
            .storage
            .with_conn(|conn| {
                let mut stmt = conn
                    .prepare("SELECT operation_id FROM creation_operations WHERE session_id=?1")?;
                let rows = stmt.query_map([SESSION], |row| row.get(0))?;
                Ok(rows.collect::<Result<Vec<_>, _>>()?)
            })
            .unwrap();
        assert_eq!(ids, vec![operation_id]);
        server.abort();
    }

    #[tokio::test]
    async fn worker_deduplicates_and_persists_batches_after_start_returns() {
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move |Json(body): Json<Value>| {
                let count = observed.clone();
                async move {
                    count.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_millis(15)).await;
                    Json(http_preparation_output(&body))
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let (_tmp, state) = fixture(None, &url);
        let first = act(&state, &request("start", 0)).await.unwrap();
        let duplicate = turn(state.clone(), &request("start", 0), None)
            .await
            .unwrap()
            .0;
        assert_eq!(duplicate.session_id, first.session_id);
        assert_eq!(first.phase, "preparing");
        assert_eq!(first.revision, 0);
        for _ in 0..5 {
            act(&state, &request("status", 0)).await.unwrap();
        }
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let session = load(&state);
                if session.phase == "exploring" {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 1);
        let saved = decode(&load(&state)).unwrap();
        let stage = saved.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.progress.completed_batches, 1);
        assert_eq!(stage.progress.percent, 100);
        assert_eq!(stage.questions.len(), 1);
        assert!(!load(&state).state_json.contains("creation_api_key"));
        server.abort();
    }

    #[tokio::test]
    async fn second_topic_duplicate_failure_retry_preserves_four_saved_questions_and_parent_refs() {
        let mut stored = initial();
        let binding = stored
            .stage_workflow
            .as_ref()
            .unwrap()
            .current()
            .root_axes_binding
            .as_ref()
            .unwrap();
        let topics = binding
            .compile_topics("direction", &["audience".into(), "purpose".into()], 6, 4)
            .unwrap();
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            json!({"topics":topics,"chapters":[],"open_flags":[]}),
        )
        .unwrap();
        apply_batch(&mut stored, four_question_tree()).unwrap();
        stored.memory_brief = "已保存来源说明".into();
        let stage = stored.stage_workflow.as_ref().unwrap().current();
        let old_generation = stage.generation_id.clone();
        let saved_questions = serde_json::to_value(&stage.questions).unwrap();
        assert_eq!(stage.questions.len(), 4);
        assert_eq!(stage.progress.completed_batches, 2);

        let duplicate_prompt = stage.questions[0].prompt.clone();
        let node_ids = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let observed_node_ids = node_ids.clone();
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move |Json(body): Json<Value>| {
                let count = observed.clone();
                let node_ids = observed_node_ids.clone();
                let duplicate_prompt = duplicate_prompt.clone();
                async move {
                    assert_eq!(body["topic"]["id"], "topic_axis_2");
                    let attempt = count.fetch_add(1, Ordering::SeqCst);
                    // Published prefix stays at four; checkpoint candidates
                    // become visible to subsequent preparation requests only.
                    assert_eq!(
                        body["plan"]["prepared_question_count"],
                        4 + attempt.saturating_sub(2)
                    );
                    assert_eq!(body["plan"]["topics"].as_array().unwrap().len(), 2);
                    if body["kind"] == "topic_plan" {
                        assert_eq!(attempt, 0); // The paid manifest/root plan is never replayed.
                        return Json(compiled_topic_roots(&body));
                    }
                    assert_eq!(body["kind"], "node");
                    node_ids
                        .lock()
                        .unwrap()
                        .push(body["node"]["id"].as_str().unwrap().into());
                    let mut output = http_node_output(&body);
                    output["memory_brief"] = json!("");
                    if attempt == 1 {
                        output["question"]["prompt"] = json!(duplicate_prompt);
                        output["memory_brief"] = json!("不得写入的失败候选来源");
                    }
                    Json(output)
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let (_tmp, state) = fixture(Some(&stored), &url);
        act(&state, &request("status", 0)).await.unwrap();
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if decode(&load(&state))
                    .unwrap()
                    .stage_workflow
                    .unwrap()
                    .current()
                    .status
                    == "failed"
                {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();

        let failed_session = load(&state);
        let failed = decode(&failed_session).unwrap();
        let stage = failed.stage_workflow.as_ref().unwrap().current();
        assert_eq!(
            stage.error.as_ref().unwrap()["code"],
            "BRAINSTORM_MODEL_OUTPUT_INVALID"
        );
        assert_eq!(stage.generation_id, old_generation);
        assert_eq!(stage.progress.completed_batches, 2);
        assert_eq!(
            serde_json::to_value(&stage.questions).unwrap(),
            saved_questions
        );
        assert_eq!(failed.memory_brief, "已保存来源说明");
        assert_eq!(count.load(Ordering::SeqCst), 2);

        let retry = act(&state, &request("retry_stage", failed_session.revision))
            .await
            .unwrap();
        let new_generation = retry
            .stage_workflow
            .unwrap()
            .current()
            .generation_id
            .clone();
        assert_ne!(new_generation, old_generation);
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if load(&state).phase == "exploring" {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();

        let recovered = decode(&load(&state)).unwrap();
        let stage = recovered.stage_workflow.as_ref().unwrap().current();
        assert_eq!(stage.progress.completed_batches, 3);
        assert_eq!(stage.questions.len(), 5);
        assert_eq!(
            serde_json::to_value(&stage.questions[..4]).unwrap(),
            saved_questions
        );
        assert!(stage.questions[..4].iter().all(|question| question
            .id
            .starts_with(&format!("stage-{}-1-", old_generation))));
        assert_eq!(stage.generation_id, new_generation);
        let mut accepted_node_ids = stage.questions[4..]
            .iter()
            .map(|q| q.id.clone())
            .collect::<Vec<_>>();
        accepted_node_ids.sort();
        let mut requested_node_ids = node_ids.lock().unwrap().clone();
        requested_node_ids.sort();
        requested_node_ids.dedup();
        assert_eq!(accepted_node_ids, requested_node_ids);
        for question in &stage.questions {
            if let Some(parent) = question.parent_question_id.as_ref() {
                assert!(stage
                    .questions
                    .iter()
                    .any(|candidate| &candidate.id == parent));
            }
        }
        assert_eq!(recovered.memory_brief, "已保存来源说明");
        assert_eq!(count.load(Ordering::SeqCst), 3);
        server.abort();
    }

    #[tokio::test]
    async fn restart_and_failed_retry_keep_paid_manifest_batch() {
        let mut stored = initial();
        let payload = batch_payload(&request("status", 0), &stored).unwrap();
        apply_preparation_step(
            &mut stored,
            &request("status", 0),
            compiled_plan_for_request(&payload),
        )
        .unwrap();
        stored.stage_workflow.as_mut().unwrap().current_mut().status = "failed".into();
        let count = Arc::new(AtomicUsize::new(0));
        let observed = count.clone();
        let router = axum::Router::new().route(
            "/creation/brainstorm/stage/batch",
            axum::routing::post(move |Json(body): Json<Value>| {
                let count = observed.clone();
                async move {
                    assert_ne!(body["kind"], "plan");
                    assert_eq!(body["plan"]["topics"].as_array().unwrap().len(), 1);
                    count.fetch_add(1, Ordering::SeqCst);
                    Json(http_preparation_output(&body))
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, router).await.unwrap();
        });
        let (_tmp, state) = fixture(Some(&stored), &url);
        let response = act(&state, &request("retry_stage", 0)).await.unwrap();
        assert_eq!(
            response
                .stage_workflow
                .unwrap()
                .current()
                .progress
                .completed_batches,
            1
        );
        tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                if load(&state).phase == "exploring" && load(&state).revision > 1 {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(count.load(Ordering::SeqCst), 2);
        server.abort();
    }
}

fn edit_saved_brief(
    stored: &mut BrainstormStoredState,
    req: &BrainstormTurnRequest,
    revision: i64,
) -> Result<String, Failure> {
    let w = stored.stage_workflow.as_ref().unwrap();
    if w.current().status == "thinking" {
        return Err(invalid("本阶段正在思考，请保留编辑草稿，待准备完成后保存"));
    }
    if req.brief_edits.iter().any(|(key, value)| {
        value.chars().count() > 12000
            || !(key == "root_request"
                || key == "open_flags"
                || stored.turns.iter().any(|t| &t.question.id == key))
    }) {
        return Err(brainstorm_error(
            StatusCode::BAD_REQUEST,
            "BRAINSTORM_INVALID_BRIEF",
            "简报字段无效或超过 12000 字",
        ));
    }
    let changes: BTreeMap<_, _> = req
        .brief_edits
        .iter()
        .filter(|(key, value)| match stored.brief_edits.get(*key) {
            Some(existing) => existing != *value,
            None if key.as_str() == "root_request" => stored.root_request != value.as_str(),
            None if key.as_str() == "open_flags" => stored.open_flags.join("\n") != value.as_str(),
            // Compare with the real displayed summary, including a first empty
            // edit that clears selected choices. Saving unchanged text is inert.
            None => stored
                .turns
                .iter()
                .find(|turn| &turn.question.id == *key)
                .is_none_or(|turn| brainstorm_effective_summary(stored, turn) != value.as_str()),
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    // The editor submits all displayed fields. Unchanged upstream fields are
    // harmless, while only the active unsubmitted chapter may still change
    // after previous chapters have been written.
    if has_protected_chapters(w) && changes.keys().any(|id| !belongs_to_editable_chapter(w, id)) {
        return Err(brainstorm_error(
            StatusCode::CONFLICT,
            "BRAINSTORM_CHAPTER_REVIEW_REQUIRED",
            "已有章节正在创作或已完成，请先回看正文后再调整上游简报",
        ));
    }
    if changes.is_empty() {
        return Ok(if w.chapters.iter().any(|c| c.status == "writing") {
            "chapter_writing"
        } else if w.current().status == "completed" {
            "completed"
        } else if stored.current_question.is_some() {
            "exploring"
        } else {
            "ready"
        }
        .into());
    }
    if let Some(root) = changes.get("root_request") {
        if root.trim().is_empty() {
            return Err(invalid("创作需求不能为空"));
        }
        // Repair evidence belongs to the session, including archived generations.
        let copy_repair_history = w.copy_repair_history.clone();
        let mut recovery_history: BTreeMap<_, _> = w
            .stages
            .iter()
            .map(|stage| (stage.id.clone(), stage.recovery_history.clone()))
            .collect();
        let mut preparation_archives: BTreeMap<_, _> = w
            .stages
            .iter()
            .map(|stage| (stage.id.clone(), stage.preparation_archives.clone()))
            .collect();
        let mut content_archives: BTreeMap<_, _> = w.stages.iter().map(|stage| {
            let mut saved = stage.clone();
            if saved.content_mode {
                archive_content_preparation(&mut saved, "root_request_changed");
            }
            (stage.id.clone(), saved.content_archives)
        }).collect();
        let stage_question_ids = w.stages.iter().flat_map(|stage|
            stage.questions.iter().map(|question| question.id.clone()))
            .collect::<HashSet<_>>();
        let submitted_revisions = changes.keys().map(|field|
            (field.clone(), revision)).collect::<BTreeMap<_, _>>();
        let mut content_brief_edit_archives: BTreeMap<_, _> = w.stages.iter().map(|stage| {
            let mut saved = stage.clone();
            let mut fields = stage.questions.iter().map(|question| question.id.clone())
                .collect::<HashSet<_>>();
            if stage.id == "direction" {
                fields.extend(stored.brief_edits.keys()
                    .filter(|field| !stage_question_ids.contains(*field)).cloned());
            }
            archive_content_brief_edits(&mut saved, &stored.brief_edits,
                &stored.user_input_revisions, &fields, "root_request_changed");
            let submitted_fields = stage.questions.iter().filter(|question|
                changes.contains_key(&question.id)).map(|question| question.id.clone())
                .collect::<HashSet<_>>();
            archive_content_brief_edits(&mut saved, &changes,
                &submitted_revisions, &submitted_fields, "root_request_changed_submitted");
            (stage.id.clone(), saved.content_brief_edit_archives)
        }).collect();
        let mut content_root_request_archives: BTreeMap<_, _> = w.stages.iter().map(|stage| {
            let mut saved = stage.clone();
            if stage.id == "direction" {
                archive_replaced_content_root_request(
                    &mut saved,
                    &stored.root_request,
                    stored.brief_edits.get("root_request"),
                    stored.user_input_revisions.get("root_request").copied(),
                    root,
                    revision,
                );
            }
            (stage.id.clone(), saved.content_root_request_archives)
        }).collect();
        let mut checkpoint_archives: BTreeMap<_, _> = w
            .stages
            .iter()
            .map(|stage| {
                let mut archives = stage.topic_checkpoint_archives.clone();
                if let Some(checkpoint) = &stage.topic_checkpoint {
                    archives.push(TopicCheckpointArchive {
                        reason: "input_changed".into(),
                        archived_at_ms: now(),
                        checkpoint: checkpoint.clone(),
                    });
                }
                (stage.id.clone(), archives)
            })
            .collect();
        // Preserve every generated/answered branch before starting a new plan.
        let answers: BTreeMap<_, _> = stored
            .turns
            .iter()
            .map(|t| (t.question.id.clone(), t.answer.clone()))
            .collect();
        for stage in &w.stages {
            for q in &stage.questions {
                stored.archived_questions.push(BrainstormArchivedQuestion {
                    question: q.clone(),
                    answer: answers.get(&q.id).cloned(),
                });
            }
        }
        stored.invalidated_question_ids = answers.keys().cloned().collect();
        stored.turns.clear();
        stored.current_question = None;
        stored.memory_brief.clear();
        if w.stages.iter().any(|stage| stage.content_mode) {
            // Old pending flags describe the invalidated request. Saved user
            // revisions are archived above before the new brief is built.
            stored.open_flags.clear();
        }
        stored.root_request = root.trim().to_string();
        stored.brief_edits.clear();
        // Root and open flags remain the only effective edits after invalidation.
        for key in ["root_request", "open_flags"] {
            if let Some(value) = changes.get(key) {
                stored.brief_edits.insert(key.into(), value.clone());
            }
        }
        stored
            .user_input_revisions
            .insert("root_request".into(), revision);
        let mut workflow = StageWorkflow::new_for_selected_skills(&stored.selected_skills);
        workflow.copy_repair_history = copy_repair_history;
        for stage in &mut workflow.stages {
            stage.recovery_history = recovery_history.remove(&stage.id).unwrap_or_default();
            stage.preparation_archives = preparation_archives.remove(&stage.id).unwrap_or_default();
            stage.content_archives = content_archives.remove(&stage.id).unwrap_or_default();
            stage.content_brief_edit_archives = content_brief_edit_archives.remove(&stage.id)
                .unwrap_or_default();
            stage.content_root_request_archives = content_root_request_archives.remove(&stage.id)
                .unwrap_or_default();
            stage.topic_checkpoint_archives =
                checkpoint_archives.remove(&stage.id).unwrap_or_default();
        }
        stored.stage_workflow = Some(workflow);
        return Ok("preparing".into());
    }
    let mut phase = if stored.current_question.is_some() {
        "exploring"
    } else {
        "ready"
    }
    .to_string();
    // Changes to later decisions are archived if an earlier edited decision
    // invalidates them. Their user text remains in the archived answer record.
    let mut edited: Vec<_> = changes
        .iter()
        .filter(|(key, _)| key.as_str() != "open_flags")
        .collect();
    edited.sort_by_key(|(id, _)| {
        std::cmp::Reverse(
            stored
                .turns
                .iter()
                .position(|t| &t.question.id == *id)
                .unwrap_or(0),
        )
    });
    for (id, value) in edited {
        if stored.turns.iter().any(|t| &t.question.id == id) {
            let mut update = req.clone();
            update.action = "revise_answer".into();
            update.question_id = Some(id.clone());
            // Editing the summary is an explicit custom answer, never a guess
            // that its old selected option still expresses the user's intent.
            update.answer = Some(BrainstormAnswer {
                selected_option_ids: vec![],
                custom_text: value.clone(),
                source: "user".into(),
            });
            if value.trim().is_empty() {
                update.action = "clear_saved_answer".into();
                update.answer = None;
            }
            phase = revise_saved_answer(stored, &update, revision)?;
            stored.brief_edits.insert(id.clone(), value.clone());
        }
    }
    if let Some(flags) = changes.get("open_flags") {
        stored
            .brief_edits
            .insert("open_flags".into(), flags.clone());
        stored
            .user_input_revisions
            .insert("open_flags".into(), revision);
    }
    if !changes.is_empty() {
        stored.memory_brief.clear();
    }
    Ok(phase)
}
