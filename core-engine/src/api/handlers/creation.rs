use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderMap, StatusCode},
    response::{sse::Event, IntoResponse, Response, Sse},
    Json,
};
use futures::stream::Stream;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    convert::Infallible,
    sync::Arc,
    time::{Duration, Instant},
};
use tracing::{error, info};

use crate::api::state::AppState;

mod brainstorm_prefetch;
pub(crate) use brainstorm_prefetch::BrainstormPrefetchCache;

#[derive(Debug, Deserialize)]
pub struct GenerateRequest {
    pub user_prompt: String,
    #[serde(default)]
    pub design_ids: Vec<i64>,
    #[serde(default)]
    pub timeline_ids: Vec<i64>,
    #[serde(default)]
    pub capture_ids: Vec<i64>,
    #[serde(default)]
    pub doc_type: String,
    #[serde(default)]
    pub audience: String,
    #[serde(default = "default_output_format")]
    pub output_format: String,
    #[serde(default = "default_true")]
    pub inherit_format: bool,
    #[serde(default = "default_true")]
    pub enable_rag: bool,
    #[serde(default)]
    pub enable_web_search: bool,
    #[serde(default)]
    pub enable_image_generation: bool,
    #[serde(default = "default_true")]
    pub browser_extension_enabled: bool,
    #[serde(default = "default_creation_tool_ids")]
    pub enabled_tools: Vec<String>,
    #[serde(default = "default_content_weight")]
    pub content_weight: f64,
    #[serde(default = "default_quality_weight")]
    pub quality_weight: f64,
    #[serde(default = "default_completeness_weight")]
    pub completeness_weight: f64,
    #[serde(default = "default_usage_weight")]
    pub usage_weight: f64,
    #[serde(default = "default_format_weight")]
    pub format_weight: f64,
    #[serde(default = "default_freshness_weight")]
    pub freshness_weight: f64,
    #[serde(default = "default_max_references")]
    pub max_references: i64,
    #[serde(default = "default_data_search_limit")]
    pub data_search_limit: usize,
    #[serde(default)]
    pub creation_model: Option<String>,
    #[serde(default)]
    pub creation_api_key: Option<String>,
    #[serde(default)]
    pub creation_base_url: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct AgentRunRequest {
    #[serde(flatten)]
    pub generation: GenerateRequest,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub run_id: Option<String>,
    #[serde(default)]
    pub instruction_id: Option<String>,
    #[serde(default)]
    pub resume_operation_id: Option<String>,
    #[serde(default)]
    pub root_request: Option<String>,
    #[serde(default)]
    pub current_document: String,
    #[serde(default)]
    pub conversation: Vec<serde_json::Value>,
    #[serde(default)]
    pub selected_skills: Vec<serde_json::Value>,
    #[serde(default)]
    pub available_skills: Option<Vec<serde_json::Value>>,
    #[serde(default)]
    pub explicit_skill_ids: Vec<String>,
    #[serde(default = "default_model_mode")]
    pub model_mode: String,
    #[serde(default)]
    pub confirmed: bool,
    #[serde(default)]
    pub resume_state: Option<serde_json::Value>,
    #[serde(default)]
    pub model_result: Option<String>,
    #[serde(default)]
    pub model_request_id: Option<String>,
    #[serde(default = "default_creation_mode")]
    pub creation_mode: String,
    #[serde(default)]
    pub creation_brief: Option<serde_json::Value>,
    #[serde(default = "default_execution_origin")]
    pub execution_origin: String,
}

const INLINE_EDIT_SCHEMA_VERSION: &str = "creation.inline-edit.v1";
const INLINE_EDIT_CONSTRAINTS_VERSION: &str = "creation.inline-edit.constraints.v1";
const INLINE_EDIT_MAX_SELECTION_BYTES: usize = 12_000;
const INLINE_EDIT_MAX_CUSTOM_PROMPT_BYTES: usize = 2_000;
const CREATION_LEASE_TTL_MS: i64 = 15 * 60 * 1000;

fn creation_sidecar_client() -> reqwest::Client {
    // The creation service is always a loopback process. Inheriting HTTP(S)_PROXY
    // here lets desktop proxy tools intercept a long-lived SSE stream and can
    // truncate it with an unexpected EOF even while both local processes live.
    reqwest::Client::builder()
        .no_proxy()
        .build()
        .expect("local creation sidecar client")
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct InlineEditSelection {
    pub base_revision_no: i64,
    pub base_document_hash: String,
    pub start_byte: usize,
    pub end_byte: usize,
    pub selected_markdown: String,
    pub selected_markdown_hash: String,
    pub selected_text: String,
    pub start_line: i64,
    pub end_line: i64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct InlineEditRequest {
    pub schema_version: String,
    pub request_id: String,
    pub session_id: String,
    pub history_id: i64,
    #[serde(default)]
    pub root_request: String,
    pub current_document: String,
    pub action: String,
    #[serde(default)]
    pub custom_prompt: String,
    pub selection: InlineEditSelection,
    #[serde(default = "default_model_mode")]
    pub model_mode: String,
    #[serde(default)]
    pub resume_state: Option<serde_json::Value>,
    #[serde(default)]
    pub model_result: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct InlineEditCapabilitiesQuery {
    #[serde(default)]
    pub history_id: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct InlineEditCapabilitiesResponse {
    pub schema_version: String,
    pub enabled: bool,
    pub actions: Vec<String>,
    pub max_selection_bytes: usize,
    pub max_custom_prompt_bytes: usize,
    pub supported_node_kinds: Vec<String>,
    pub history_id: Option<i64>,
    pub revision_no: Option<i64>,
    pub base_document_hash: Option<String>,
    pub disabled_reason: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct InlineEditResponse {
    pub schema_version: String,
    pub request_id: String,
    pub status: String,
    pub operation_fingerprint: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub replacement_markdown: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub revision_no: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub patch: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub model_request: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resume_state: Option<serde_json::Value>,
}

#[derive(Debug, Deserialize)]
pub struct InlineEditCancelRequest {
    pub request_id: String,
    pub session_id: String,
}

#[derive(Debug, Deserialize)]
pub struct InlineEditUndoRequest {
    pub request_id: String,
    pub session_id: String,
    pub history_id: i64,
    pub expected_result_hash: String,
}

#[derive(Debug, Serialize)]
pub struct InlineEditErrorBody {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    pub request_id: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct InlineEditErrorEnvelope {
    pub error: InlineEditErrorBody,
}

type InlineEditHttpError = (StatusCode, Json<InlineEditErrorEnvelope>);

#[derive(Debug, Serialize)]
struct InlineEditConstraintsPayload {
    schema_version: String,
    allowed_facts: Vec<String>,
    source_ids: Vec<String>,
    skill_invariants: Vec<String>,
}

#[derive(Debug, Serialize)]
struct InlineEditSidecarPayload {
    schema_version: String,
    request_id: String,
    action: String,
    selected_markdown: String,
    section_context: String,
    custom_prompt: String,
    model_mode: String,
    context_constraints: InlineEditConstraintsPayload,
    resume_state: Option<serde_json::Value>,
    model_result: Option<String>,
}

#[derive(Debug, Deserialize)]
struct InlineEditSidecarResponse {
    status: String,
    #[serde(default)]
    replacement_markdown: Option<String>,
    #[serde(default)]
    model_request: Option<serde_json::Value>,
    #[serde(default)]
    resume_state: Option<serde_json::Value>,
}

fn inline_edit_error(
    status: StatusCode,
    code: &str,
    message: &str,
    retryable: bool,
    request_id: Option<&str>,
) -> InlineEditHttpError {
    (
        status,
        Json(InlineEditErrorEnvelope {
            error: InlineEditErrorBody {
                code: code.to_string(),
                message: message.to_string(),
                retryable,
                request_id: request_id.map(ToOwned::to_owned),
            },
        }),
    )
}

fn sha256_hex(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

fn canonical_json(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::Object(map) => {
            let mut keys = map.keys().collect::<Vec<_>>();
            keys.sort();
            let body = keys
                .into_iter()
                .map(|key| {
                    format!(
                        "{}:{}",
                        serde_json::to_string(key).unwrap_or_default(),
                        canonical_json(&map[key])
                    )
                })
                .collect::<Vec<_>>()
                .join(",");
            format!("{{{body}}}")
        }
        serde_json::Value::Array(items) => {
            format!(
                "[{}]",
                items
                    .iter()
                    .map(canonical_json)
                    .collect::<Vec<_>>()
                    .join(",")
            )
        }
        _ => serde_json::to_string(value).unwrap_or_else(|_| "null".to_string()),
    }
}

fn inline_operation(action: &str) -> Option<&'static str> {
    match action {
        "brainstorm" => Some("brainstorm_selection"),
        "polish" => Some("polish_selection"),
        "expand" => Some("expand_selection"),
        "elaborate" => Some("elaborate_selection"),
        _ => None,
    }
}

fn acquire_creation_lease(state: &AppState, session_id: &str, owner: &str) -> bool {
    let now = chrono::Utc::now().timestamp_millis();
    let mut leases = state
        .creation_session_leases
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    leases.retain(|_, lease| now - lease.updated_at_ms <= CREATION_LEASE_TTL_MS);
    if let Some(lease) = leases.get_mut(session_id) {
        if lease.owner_request_id != owner {
            return false;
        }
        lease.updated_at_ms = now;
        return true;
    }
    leases.insert(
        session_id.to_string(),
        crate::api::state::CreationSessionLease {
            owner_request_id: owner.to_string(),
            updated_at_ms: now,
        },
    );
    true
}

fn touch_creation_lease(state: &AppState, session_id: &str, owner: &str) {
    let now = chrono::Utc::now().timestamp_millis();
    let mut leases = state
        .creation_session_leases
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if let Some(lease) = leases
        .get_mut(session_id)
        .filter(|lease| lease.owner_request_id == owner)
    {
        lease.updated_at_ms = now;
    }
}

fn release_creation_lease(state: &AppState, session_id: &str, owner: &str) {
    let mut leases = state
        .creation_session_leases
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if leases
        .get(session_id)
        .is_some_and(|lease| lease.owner_request_id == owner)
    {
        leases.remove(session_id);
    }
}

struct CreationLeaseGuard {
    state: Arc<AppState>,
    session_id: Option<String>,
    operation_id: Option<String>,
    owner: String,
}

impl Drop for CreationLeaseGuard {
    fn drop(&mut self) {
        if let Some(session_id) = self.session_id.as_deref() {
            if let Some(operation_id) = self.operation_id.as_deref() {
                if let Err(error) = self.state.storage.with_conn(|conn| {
                    crate::storage::repo::creation_operation::mark_waiting_if_running(
                        conn,
                        session_id,
                        operation_id,
                    )
                    .map_err(Into::into)
                }) {
                    error!("创作流断开后的可恢复状态保存失败: {}", error);
                }
            }
            release_creation_lease(&self.state, session_id, &self.owner);
        }
    }
}

fn inline_section_context(document: &str, start_line: i64, end_line: i64) -> String {
    let lines = document.lines().collect::<Vec<_>>();
    if lines.is_empty() {
        return String::new();
    }
    let start_index = (start_line.max(1) as usize - 1).min(lines.len() - 1);
    let end_index = (end_line.max(start_line).max(1) as usize).min(lines.len());
    let section_start = (0..=start_index)
        .rev()
        .find(|index| lines[*index].trim_start().starts_with('#'))
        .unwrap_or(start_index.saturating_sub(3));
    let section_end = (end_index..lines.len())
        .find(|index| lines[*index].trim_start().starts_with('#'))
        .unwrap_or_else(|| (end_index + 8).min(lines.len()));
    lines[section_start..section_end]
        .join("\n")
        .chars()
        .take(8_000)
        .collect()
}

fn inline_constraints(
    history: &crate::storage::repo::creation_history::CreationHistory,
    section_context: &str,
) -> InlineEditConstraintsPayload {
    let allowed_facts = section_context
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .take(40)
        .map(|line| line.chars().take(1_000).collect())
        .collect::<Vec<String>>();
    let source_ids = history
        .evidence_json
        .as_deref()
        .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|item| {
            item.get("id")
                .and_then(|value| value.as_str())
                .map(|value| value.chars().take(200).collect())
        })
        .take(40)
        .collect();
    InlineEditConstraintsPayload {
        schema_version: INLINE_EDIT_CONSTRAINTS_VERSION.to_string(),
        allowed_facts,
        source_ids,
        skill_invariants: Vec::new(),
    }
}

fn inline_operation_fingerprint(
    req: &InlineEditRequest,
    constraints: &InlineEditConstraintsPayload,
) -> String {
    let constraints_value = serde_json::to_value(constraints).unwrap_or_default();
    let value = serde_json::json!({
        "schema_version": req.schema_version,
        "session_id": req.session_id,
        "history_id": req.history_id,
        "action": req.action,
        "model_mode": req.model_mode,
        "base_revision_no": req.selection.base_revision_no,
        "base_document_hash": req.selection.base_document_hash,
        "start_byte": req.selection.start_byte,
        "end_byte": req.selection.end_byte,
        "selected_markdown_hash": req.selection.selected_markdown_hash,
        "custom_prompt_hash": sha256_hex(&req.custom_prompt),
        "root_request_hash": sha256_hex(&req.root_request),
        "constraints_hash": sha256_hex(&canonical_json(&constraints_value)),
    });
    sha256_hex(&canonical_json(&value))
}

fn append_history_json_item(raw: Option<&str>, item: serde_json::Value) -> String {
    let mut items = raw
        .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
        .unwrap_or_default();
    items.push(item);
    serde_json::to_string(&items).unwrap_or_else(|_| "[]".to_string())
}

fn inline_summary(action: &str) -> &'static str {
    match action {
        "brainstorm" => "按脑暴结论改写所选内容",
        "polish" => "润色所选内容",
        "expand" => "扩充所选内容",
        "elaborate" => "细化所选内容",
        _ => "修改所选内容",
    }
}

fn inline_user_instruction(action: &str, selected_text: &str, custom_prompt: &str) -> String {
    let (action_label, requirement) = match action {
        "brainstorm" => (
            "脑暴写回",
            "按本轮已确认的局部脑暴结论改写所选内容，不超出事实与选区边界",
        ),
        "polish" => ("润色", "改善所选内容的措辞、语气和连贯性，不新增事实"),
        "expand" => (
            "扩充",
            "基于已有上下文扩充所选内容，补充解释、过渡和事实支持",
        ),
        "elaborate" => (
            "细化",
            "细化所选内容，补齐对象、条件、步骤、边界、风险或验收维度",
        ),
        _ => ("修改", "按用户要求修改所选内容"),
    };
    let normalized = selected_text
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let mut excerpt = normalized.chars().take(600).collect::<String>();
    if normalized.chars().count() > 600 {
        excerpt.push('…');
    }
    let mut lines = vec![format!("{action_label}要求：{requirement}")];
    if !excerpt.is_empty() {
        lines.push(format!("选取内容：{excerpt}"));
    }
    if !custom_prompt.trim().is_empty() {
        lines.push(format!("补充要求：{}", custom_prompt.trim()));
    }
    lines.join("\n")
}

#[derive(Debug, Serialize)]
struct CreationPayload {
    user_prompt: String,
    design_templates: Vec<serde_json::Value>,
    timeline_context: Option<String>,
    capture_context: Option<String>,
    doc_type: String,
    audience: String,
    output_format: String,
    inherit_format: bool,
    enable_rag: bool,
    enable_web_search: bool,
    enable_image_generation: bool,
    browser_extension_enabled: bool,
    enabled_tools: Vec<String>,
    content_weight: f64,
    quality_weight: f64,
    completeness_weight: f64,
    usage_weight: f64,
    format_weight: f64,
    freshness_weight: f64,
    max_references: i64,
    data_search_limit: usize,
    creation_model: Option<String>,
    creation_api_key: Option<String>,
    creation_base_url: Option<String>,
}

#[derive(Debug, Serialize)]
struct AgentRunPayload {
    #[serde(flatten)]
    creation: CreationPayload,
    session_id: Option<String>,
    run_id: Option<String>,
    root_request: Option<String>,
    current_document: String,
    conversation: Vec<serde_json::Value>,
    selected_skills: Vec<serde_json::Value>,
    available_skills: Option<Vec<serde_json::Value>>,
    explicit_skill_ids: Vec<String>,
    model_mode: String,
    confirmed: bool,
    resume_state: Option<serde_json::Value>,
    model_result: Option<String>,
    creation_mode: String,
    creation_brief: Option<serde_json::Value>,
    execution_origin: String,
    operation_context: Option<serde_json::Value>,
    resume_checkpoint: Option<serde_json::Value>,
}

#[derive(Debug, Serialize)]
struct ReferencePayload {
    user_prompt: String,
    doc_type: String,
    audience: String,
    inherit_format: bool,
    enable_rag: bool,
    content_weight: f64,
    quality_weight: f64,
    completeness_weight: f64,
    usage_weight: f64,
    format_weight: f64,
    freshness_weight: f64,
    max_references: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrainstormOption {
    pub id: String,
    pub label: String,
    pub description: String,
    #[serde(default)]
    pub details: String,
    #[serde(default)]
    pub recommended: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrainstormContinuationDirection {
    pub id: String,
    pub label: String,
    pub description: String,
    #[serde(default)]
    pub details: String,
    #[serde(default)]
    pub recommended: bool,
}

impl From<&BrainstormContinuationDirection> for BrainstormOption {
    fn from(direction: &BrainstormContinuationDirection) -> Self {
        Self {
            id: direction.id.clone(),
            label: direction.label.clone(),
            description: direction.description.clone(),
            details: direction.details.clone(),
            recommended: direction.recommended,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrainstormQuestion {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exploration_stage: Option<String>,
    #[serde(default)]
    pub parent_question_id: Option<String>,
    #[serde(default)]
    pub parent_option_id: Option<String>,
    #[serde(default)]
    pub single_choice_reason: String,
    pub id: String,
    #[serde(default)]
    pub dimension_id: String,
    pub dimension: String,
    #[serde(rename = "type")]
    pub question_type: String,
    pub prompt: String,
    pub why_now: String,
    #[serde(default)]
    pub context_details: String,
    pub required: bool,
    pub allow_custom: bool,
    #[serde(default)]
    pub options: Vec<BrainstormOption>,
    #[serde(default)]
    pub answer_template: String,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BrainstormAnswer {
    #[serde(default)]
    pub selected_option_ids: Vec<String>,
    #[serde(default)]
    pub custom_text: String,
    #[serde(default = "default_brainstorm_answer_source")]
    pub source: String,
}

#[derive(Debug, Serialize)]
pub struct BrainstormDecision {
    pub question_id: String,
    pub dimension_id: String,
    pub dimension: String,
    pub summary: String,
    pub source: String,
    pub user_inputs: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct BrainstormTurnHistoryItem {
    pub question: BrainstormQuestion,
    pub answer: BrainstormAnswer,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrainstormArchivedQuestion {
    question: BrainstormQuestion,
    answer: Option<BrainstormAnswer>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct BrainstormStoredState {
    #[serde(default)]
    pending_extensions: Vec<BrainstormExtensionPlan>,
    #[serde(default)]
    current_extension_id: Option<String>,
    #[serde(default)]
    prefetch_safe_options: std::collections::BTreeMap<String, Vec<String>>,
    #[serde(default)]
    branch_parent_revisions: std::collections::BTreeMap<String, i64>,
    #[serde(default)]
    memory_brief: String,
    #[serde(default)]
    archived_questions: Vec<BrainstormArchivedQuestion>,
    #[serde(default)]
    brief_edits: std::collections::BTreeMap<String, String>,
    #[serde(default)]
    user_input_revisions: std::collections::BTreeMap<String, i64>,
    root_request: String,
    #[serde(default)]
    selected_skills: Vec<serde_json::Value>,
    #[serde(default)]
    turns: Vec<BrainstormStoredTurn>,
    #[serde(default)]
    current_question: Option<BrainstormQuestion>,
    #[serde(default)]
    open_flags: Vec<String>,
    #[serde(default)]
    readiness_reason: String,
    #[serde(default)]
    continuation_directions: Vec<BrainstormContinuationDirection>,
    #[serde(default)]
    invalidated_question_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct BrainstormExtensionPlan {
    id: String,
    parent_question_id: String,
    parent_option_id: Option<String>,
    parent_revision: i64,
    goal: String,
    ordinal: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct BrainstormStoredTurn {
    question: BrainstormQuestion,
    answer: BrainstormAnswer,
}

#[derive(Debug, Clone, Deserialize)]
pub struct BrainstormTurnRequest {
    #[serde(default)]
    pub brief_edits: std::collections::BTreeMap<String, String>,
    pub session_id: String,
    pub root_request: String,
    pub action: String,
    #[serde(default)]
    pub selected_skills: Vec<serde_json::Value>,
    #[serde(default)]
    pub revision: Option<i64>,
    #[serde(default)]
    pub question_id: Option<String>,
    #[serde(default)]
    pub answer: Option<BrainstormAnswer>,
    #[serde(default)]
    pub accept_assumptions: bool,
    #[serde(default)]
    pub focus_hint: String,
    #[serde(default)]
    pub continuation_direction_id: String,
    #[serde(default)]
    pub continuation_direction_ids: Vec<String>,
    #[serde(default)]
    pub creation_model: Option<String>,
    #[serde(default)]
    pub creation_api_key: Option<String>,
    #[serde(default)]
    pub creation_base_url: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct BrainstormTurnResponse {
    pub archived_questions: Vec<BrainstormArchivedQuestion>,
    pub root_request: String,
    pub brief_edits: std::collections::BTreeMap<String, String>,
    pub user_input_revisions: std::collections::BTreeMap<String, i64>,
    pub session_id: String,
    pub phase: String,
    pub revision: i64,
    pub current_question: Option<BrainstormQuestion>,
    pub brief_markdown: String,
    pub answered_count: usize,
    pub depth: usize,
    pub can_continue_brainstorm: bool,
    pub open_flags: Vec<String>,
    pub readiness_reason: String,
    pub continuation_directions: Vec<BrainstormContinuationDirection>,
    pub invalidated_question_ids: Vec<String>,
    pub decisions: Vec<BrainstormDecision>,
    pub history: Vec<BrainstormTurnHistoryItem>,
}

pub async fn get_inline_edit_capabilities(
    State(state): State<Arc<AppState>>,
    Query(query): Query<InlineEditCapabilitiesQuery>,
) -> Result<Json<InlineEditCapabilitiesResponse>, InlineEditHttpError> {
    let mut response = InlineEditCapabilitiesResponse {
        schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
        enabled: false,
        actions: Vec::new(),
        max_selection_bytes: INLINE_EDIT_MAX_SELECTION_BYTES,
        max_custom_prompt_bytes: INLINE_EDIT_MAX_CUSTOM_PROMPT_BYTES,
        supported_node_kinds: vec!["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"]
            .into_iter()
            .map(ToOwned::to_owned)
            .collect(),
        history_id: query.history_id,
        revision_no: None,
        base_document_hash: None,
        disabled_reason: None,
    };

    let Some(history_id) = query.history_id else {
        response.disabled_reason = Some("文档尚未建立可写历史".to_string());
        return Ok(Json(response));
    };
    let history = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_history::get_by_id(conn, history_id).map_err(Into::into)
        })
        .map_err(|_| {
            inline_edit_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "读取创作历史失败",
                true,
                None,
            )
        })?
        .ok_or_else(|| {
            inline_edit_error(
                StatusCode::NOT_FOUND,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "创作文档不存在",
                false,
                None,
            )
        })?;
    response.revision_no = Some(history.revision_no);
    response.base_document_hash = Some(sha256_hex(&history.generated_content));
    if history.lifecycle_status != "completed" || history.generated_content.trim().is_empty() {
        response.disabled_reason = Some("文档仍在生成或尚无可编辑正文".to_string());
        return Ok(Json(response));
    }
    let sidecar_url = format!(
        "{}/creation/inline-edit/capabilities",
        state.creation_sidecar_url.trim_end_matches('/')
    );
    let sidecar = creation_sidecar_client()
        .get(sidecar_url)
        .timeout(Duration::from_secs(5))
        .send()
        .await;
    let Ok(sidecar) = sidecar else {
        response.disabled_reason = Some("选区编辑服务暂不可用".to_string());
        return Ok(Json(response));
    };
    if !sidecar.status().is_success() {
        response.disabled_reason = Some("选区编辑服务版本不匹配".to_string());
        return Ok(Json(response));
    }
    let sidecar_capabilities = sidecar
        .json::<InlineEditCapabilitiesResponse>()
        .await
        .unwrap_or(InlineEditCapabilitiesResponse {
            schema_version: String::new(),
            enabled: false,
            actions: Vec::new(),
            max_selection_bytes: INLINE_EDIT_MAX_SELECTION_BYTES,
            max_custom_prompt_bytes: INLINE_EDIT_MAX_CUSTOM_PROMPT_BYTES,
            supported_node_kinds: Vec::new(),
            history_id: None,
            revision_no: None,
            base_document_hash: None,
            disabled_reason: None,
        });
    if sidecar_capabilities.schema_version != INLINE_EDIT_SCHEMA_VERSION
        || !sidecar_capabilities.enabled
    {
        response.disabled_reason = Some("选区编辑服务版本不匹配".to_string());
        return Ok(Json(response));
    }
    response.enabled = true;
    response.actions = sidecar_capabilities.actions;
    response.max_selection_bytes = response
        .max_selection_bytes
        .min(sidecar_capabilities.max_selection_bytes);
    response.max_custom_prompt_bytes = response
        .max_custom_prompt_bytes
        .min(sidecar_capabilities.max_custom_prompt_bytes);
    response.supported_node_kinds = sidecar_capabilities.supported_node_kinds;
    Ok(Json(response))
}

fn inline_replay_response(
    run: crate::storage::repo::creation_inline_edit::CreationInlineEditRun,
) -> Option<InlineEditResponse> {
    if run.status != "committed" {
        return None;
    }
    Some(InlineEditResponse {
        schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
        request_id: run.request_id,
        status: "committed".to_string(),
        operation_fingerprint: run.operation_fingerprint,
        content: run.result_content,
        replacement_markdown: run.replacement_markdown,
        revision_no: run.result_revision_no,
        patch: run
            .document_patch_json
            .and_then(|value| serde_json::from_str(&value).ok()),
        model_request: None,
        resume_state: None,
    })
}

pub async fn run_creation_inline_edit(
    State(state): State<Arc<AppState>>,
    Json(req): Json<InlineEditRequest>,
) -> Result<Json<InlineEditResponse>, InlineEditHttpError> {
    let request_id = req.request_id.trim().to_string();
    let fail = |status, code, message, retryable| {
        inline_edit_error(
            status,
            code,
            message,
            retryable,
            (!request_id.is_empty()).then_some(request_id.as_str()),
        )
    };
    if req.schema_version != INLINE_EDIT_SCHEMA_VERSION
        || request_id.len() < 8
        || request_id.len() > 128
        || req.session_id.trim().is_empty()
    {
        return Err(fail(
            StatusCode::BAD_REQUEST,
            "CREATION_INLINE_EDIT_INVALID",
            "选区编辑请求无效",
            false,
        ));
    }
    let operation = inline_operation(&req.action).ok_or_else(|| {
        fail(
            StatusCode::BAD_REQUEST,
            "CREATION_INLINE_EDIT_INVALID",
            "不支持的选区编辑动作",
            false,
        )
    })?;
    if !matches!(req.model_mode.as_str(), "local" | "external")
        || (!matches!(req.action.as_str(), "brainstorm" | "polish")
            && !req.custom_prompt.trim().is_empty())
        || req.custom_prompt.as_bytes().len() > INLINE_EDIT_MAX_CUSTOM_PROMPT_BYTES
    {
        return Err(fail(
            StatusCode::BAD_REQUEST,
            "CREATION_INLINE_EDIT_INVALID",
            "选区编辑参数不符合契约",
            false,
        ));
    }

    let history = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_history::get_by_id(conn, req.history_id)
                .map_err(Into::into)
        })
        .map_err(|_| {
            fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "读取创作历史失败",
                true,
            )
        })?
        .ok_or_else(|| {
            fail(
                StatusCode::NOT_FOUND,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "创作文档不存在",
                false,
            )
        })?;
    if !crate::storage::repo::creation_history::matches_document_base(&history, req.session_id.trim(), req.selection.base_revision_no, &req.current_document)
        || sha256_hex(&history.generated_content) != req.selection.base_document_hash
    {
        return Err(fail(
            StatusCode::CONFLICT,
            "CREATION_SELECTION_BASE_CHANGED",
            "文档已经变化，请重新划选",
            false,
        ));
    }
    if history.lifecycle_status != "completed" {
        return Err(fail(
            StatusCode::UNPROCESSABLE_ENTITY,
            "CREATION_SELECTION_UNSUPPORTED",
            "当前文档仍在生成，暂不支持选区编辑",
            false,
        ));
    }
    let selection = history
        .generated_content
        .get(req.selection.start_byte..req.selection.end_byte)
        .ok_or_else(|| {
            fail(
                StatusCode::BAD_REQUEST,
                "CREATION_INLINE_EDIT_INVALID",
                "选区字节范围无效",
                false,
            )
        })?;
    if selection != req.selection.selected_markdown
        || sha256_hex(selection) != req.selection.selected_markdown_hash
        || selection.trim().is_empty()
        || selection.as_bytes().len() > INLINE_EDIT_MAX_SELECTION_BYTES
    {
        return Err(fail(
            StatusCode::BAD_REQUEST,
            "CREATION_INLINE_EDIT_INVALID",
            "选区内容与基线不一致",
            false,
        ));
    }
    if selection.contains("memorybread:")
        || selection.contains("<!--")
        || selection.contains("```")
        || selection.contains("~~~")
        || selection.contains('\0')
        || selection.matches("__").count() % 2 != 0
        || selection.matches("~~").count() % 2 != 0
        || selection.matches('`').count() % 2 != 0
    {
        return Err(fail(
            StatusCode::UNPROCESSABLE_ENTITY,
            "CREATION_SELECTION_UNSUPPORTED",
            "当前选区跨越受保护的 Markdown 结构",
            false,
        ));
    }

    let section_context = inline_section_context(
        &history.generated_content,
        req.selection.start_line,
        req.selection.end_line,
    );
    let constraints = inline_constraints(&history, &section_context);
    let operation_fingerprint = inline_operation_fingerprint(&req, &constraints);

    // A paused external-model phase is persisted so the same request can
    // resume safely. If the caller disappears, however, that row must not lock
    // the whole creation session forever. Keep the durable run lifecycle in
    // sync with the in-memory lease TTL before checking idempotency or session
    // activity; committing rows are intentionally excluded in the repository.
    let stale_before_ms = chrono::Utc::now()
        .timestamp_millis()
        .saturating_sub(CREATION_LEASE_TTL_MS);
    state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::cancel_stale_precommit_for_session(
                conn,
                req.session_id.trim(),
                stale_before_ms,
            )
            .map(|_| ())
            .map_err(Into::into)
        })
        .map_err(|_| {
            fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "清理过期选区运行失败",
                true,
            )
        })?;

    let existing = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::get_by_request(conn, &request_id)
                .map_err(Into::into)
        })
        .map_err(|_| {
            fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "读取选区运行状态失败",
                true,
            )
        })?;
    if let Some(existing_run) = existing.as_ref() {
        if existing_run.operation_fingerprint != operation_fingerprint {
            return Err(fail(
                StatusCode::CONFLICT,
                "CREATION_INLINE_EDIT_IDEMPOTENCY_CONFLICT",
                "同一请求 ID 对应了不同操作",
                false,
            ));
        }
        if let Some(response) = inline_replay_response(existing_run.clone()) {
            return Ok(Json(response));
        }
        if existing_run.status == "cancelled" || existing_run.status == "undone" {
            return Ok(Json(InlineEditResponse {
                schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
                request_id: request_id.clone(),
                status: existing_run.status.clone(),
                operation_fingerprint: operation_fingerprint.clone(),
                content: None,
                replacement_markdown: None,
                revision_no: existing_run.result_revision_no,
                patch: None,
                model_request: None,
                resume_state: None,
            }));
        }
        if existing_run.status != "paused" || req.model_result.is_none() {
            return Err(fail(
                StatusCode::CONFLICT,
                "CREATION_INLINE_EDIT_BUSY",
                "该选区操作仍在运行",
                true,
            ));
        }
    } else {
        let active = state
            .storage
            .with_conn(|conn| {
                crate::storage::repo::creation_inline_edit::get_active_for_session(
                    conn,
                    req.session_id.trim(),
                )
                .map_err(Into::into)
            })
            .map_err(|_| {
                fail(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "CREATION_INLINE_EDIT_UNAVAILABLE",
                    "读取会话运行状态失败",
                    true,
                )
            })?;
        if active.is_some() {
            return Err(fail(
                StatusCode::CONFLICT,
                "CREATION_INLINE_EDIT_BUSY",
                "当前创作会话已有操作正在运行",
                true,
            ));
        }
    }

    if !acquire_creation_lease(&state, req.session_id.trim(), &request_id) {
        return Err(fail(
            StatusCode::CONFLICT,
            "CREATION_INLINE_EDIT_BUSY",
            "当前创作会话已有操作正在运行",
            true,
        ));
    }
    if existing.is_none() {
        if state
            .storage
            .with_conn(|conn| {
                crate::storage::repo::creation_inline_edit::insert_running(
                    conn,
                    &request_id,
                    req.session_id.trim(),
                    req.history_id,
                    &operation_fingerprint,
                    &req.action,
                    req.selection.base_revision_no,
                    &req.selection.base_document_hash,
                    &req.current_document,
                )
                .map_err(Into::into)
            })
            .is_err()
        {
            release_creation_lease(&state, req.session_id.trim(), &request_id);
            return Err(fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "创建选区运行失败",
                true,
            ));
        }
    }

    let sidecar_payload = InlineEditSidecarPayload {
        schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
        request_id: request_id.clone(),
        action: req.action.clone(),
        selected_markdown: req.selection.selected_markdown.clone(),
        section_context,
        custom_prompt: req.custom_prompt.clone(),
        model_mode: req.model_mode.clone(),
        context_constraints: constraints,
        resume_state: req.resume_state.clone(),
        model_result: req.model_result.clone(),
    };
    let sidecar_response = creation_sidecar_client()
        .post(format!(
            "{}/creation/inline-edit/run",
            state.creation_sidecar_url.trim_end_matches('/')
        ))
        .timeout(Duration::from_secs(360))
        .json(&sidecar_payload)
        .send()
        .await;
    let sidecar_response = match sidecar_response {
        Ok(response) if response.status().is_success() => response,
        Ok(response) => {
            let code = if response.status() == reqwest::StatusCode::UNPROCESSABLE_ENTITY {
                "CREATION_INLINE_EDIT_PATCH_INVALID"
            } else {
                "CREATION_INLINE_EDIT_UNAVAILABLE"
            };
            let _ = state.storage.with_conn(|conn| {
                crate::storage::repo::creation_inline_edit::set_status(
                    conn,
                    &request_id,
                    "failed",
                    Some(code),
                )
                .map_err(Into::into)
            });
            release_creation_lease(&state, req.session_id.trim(), &request_id);
            return Err(fail(
                StatusCode::BAD_GATEWAY,
                code,
                "选区编辑未生成可安全应用的结果",
                true,
            ));
        }
        Err(_) => {
            let _ = state.storage.with_conn(|conn| {
                crate::storage::repo::creation_inline_edit::set_status(
                    conn,
                    &request_id,
                    "failed",
                    Some("CREATION_INLINE_EDIT_UNAVAILABLE"),
                )
                .map_err(Into::into)
            });
            release_creation_lease(&state, req.session_id.trim(), &request_id);
            return Err(fail(
                StatusCode::SERVICE_UNAVAILABLE,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "选区编辑服务暂不可用",
                true,
            ));
        }
    };
    let sidecar_data = sidecar_response
        .json::<InlineEditSidecarResponse>()
        .await
        .map_err(|_| {
            release_creation_lease(&state, req.session_id.trim(), &request_id);
            fail(
                StatusCode::BAD_GATEWAY,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "选区编辑服务响应无效",
                true,
            )
        })?;
    if sidecar_data.status == "paused" {
        let _ = state.storage.with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::set_status(
                conn,
                &request_id,
                "paused",
                None,
            )
            .map_err(Into::into)
        });
        return Ok(Json(InlineEditResponse {
            schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
            request_id: request_id.clone(),
            status: "paused".to_string(),
            operation_fingerprint: operation_fingerprint.clone(),
            content: None,
            replacement_markdown: None,
            revision_no: None,
            patch: None,
            model_request: sidecar_data.model_request,
            resume_state: sidecar_data.resume_state,
        }));
    }
    let replacement = sidecar_data
        .replacement_markdown
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            release_creation_lease(&state, req.session_id.trim(), &request_id);
            fail(
                StatusCode::BAD_GATEWAY,
                "CREATION_INLINE_EDIT_EMPTY_RESULT",
                "选区编辑没有返回内容",
                true,
            )
        })?;
    let selected_emphasis_count = req.selection.selected_markdown.matches("**").count();
    let expected_emphasis_count = if selected_emphasis_count % 2 == 0 {
        selected_emphasis_count
    } else {
        0
    };
    if replacement.contains("memorybread:")
        || replacement.contains("<!--")
        || replacement.contains("```")
        || replacement.contains("~~~")
        || replacement.matches("**").count() != expected_emphasis_count
        || replacement.matches('`').count() % 2 != 0
    {
        release_creation_lease(&state, req.session_id.trim(), &request_id);
        return Err(fail(
            StatusCode::BAD_GATEWAY,
            "CREATION_INLINE_EDIT_PATCH_INVALID",
            "替换内容未通过 Markdown 完整性校验",
            false,
        ));
    }
    let candidate_saved = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::set_candidate(
                conn,
                &request_id,
                &replacement,
            )
            .map_err(Into::into)
        })
        .unwrap_or(false);
    if !candidate_saved {
        release_creation_lease(&state, req.session_id.trim(), &request_id);
        return Ok(Json(InlineEditResponse {
            schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
            request_id: request_id.clone(),
            status: "cancelled".to_string(),
            operation_fingerprint: operation_fingerprint.clone(),
            content: None,
            replacement_markdown: None,
            revision_no: None,
            patch: None,
            model_request: None,
            resume_state: None,
        }));
    }
    if replacement == req.selection.selected_markdown {
        let _ = state.storage.with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::set_status(
                conn,
                &request_id,
                "no_change",
                None,
            )
            .map_err(Into::into)
        });
        release_creation_lease(&state, req.session_id.trim(), &request_id);
        return Ok(Json(InlineEditResponse {
            schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
            request_id: request_id.clone(),
            status: "no_change".to_string(),
            operation_fingerprint: operation_fingerprint.clone(),
            content: Some(req.current_document),
            replacement_markdown: Some(replacement),
            revision_no: Some(req.selection.base_revision_no),
            patch: None,
            model_request: None,
            resume_state: None,
        }));
    }

    let prefix = req.current_document[..req.selection.start_byte].to_string();
    let suffix = req.current_document[req.selection.end_byte..].to_string();
    let result_content = format!("{prefix}{replacement}{suffix}");
    let result_hash = sha256_hex(&result_content);
    let replacement_hash = sha256_hex(&replacement);
    let replacement_end_byte = req.selection.start_byte + replacement.as_bytes().len();
    let summary = format!("已{}，其余文档保持不变", inline_summary(&req.action));
    let patch = serde_json::json!({
        "operation": operation,
        "target_sections": [],
        "base_hash": req.selection.base_document_hash.clone(),
        "result_hash": result_hash.clone(),
        "preserved_untouched": true,
        "selection": {
            "start_byte": req.selection.start_byte,
            "end_byte": req.selection.end_byte,
            "selected_markdown_hash": req.selection.selected_markdown_hash.clone(),
        },
        "replacement": {
            "start_byte": req.selection.start_byte,
            "end_byte": replacement_end_byte,
            "replacement_markdown_hash": replacement_hash.clone(),
        },
        "prefix_hash": sha256_hex(&prefix),
        "suffix_hash": sha256_hex(&suffix),
        "changes": [{
            "change_type": "modified",
            "section_title": "所选内容",
            "start_line": req.selection.start_line,
            "end_line": req.selection.end_line,
            "summary": inline_summary(&req.action),
        }],
        "change_count": 1,
        "summary": summary.clone(),
    });
    let now = chrono::Utc::now().timestamp_millis();
    let prompt = inline_user_instruction(
        &req.action,
        &req.selection.selected_text,
        &req.custom_prompt,
    );
    let conversation_once = append_history_json_item(
        history.conversation_json.as_deref(),
        serde_json::json!({
            "id": format!("inline-user-{now}"),
            "role": "user",
            "content": prompt,
            "createdAt": now,
            "runId": request_id.clone(),
            "inlineEdit": {"action": req.action.clone(), "requestId": request_id.clone()},
        }),
    );
    let conversation_json = append_history_json_item(
        Some(&conversation_once),
        serde_json::json!({
            "id": format!("inline-assistant-{now}"),
            "role": "assistant",
            "content": format!("已完成{}。", inline_summary(&req.action)),
            "createdAt": now + 1,
            "runId": request_id.clone(),
            "inlineEdit": {"action": req.action.clone(), "requestId": request_id.clone()},
        }),
    );
    let trace_json = append_history_json_item(
        history.agent_trace_json.as_deref(),
        serde_json::json!({
            "event_id": format!("inline-patch-{request_id}"),
            "session_id": req.session_id.trim(),
            "run_id": request_id.clone(),
            "sequence": 1,
            "timestamp": now,
            "schema_version": INLINE_EDIT_SCHEMA_VERSION,
            "type": "document.patch.applied",
            "status": "completed",
            "actor": {"kind": "agent", "id": "inline_edit_agent", "name": "选区编辑"},
            "summary": inline_summary(&req.action),
            "environment_patch": {},
            "data": {"patch": patch.clone()},
        }),
    );
    let patch_json = serde_json::to_string(&patch).unwrap_or_else(|_| "{}".to_string());
    let revision_no = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::commit_result(
                conn,
                &request_id,
                req.history_id,
                req.session_id.trim(),
                req.selection.base_revision_no,
                &req.current_document,
                &result_content,
                &conversation_json,
                &trace_json,
                operation,
                &patch_json,
                &result_hash,
            )
            .map_err(Into::into)
        })
        .map_err(|_| {
            release_creation_lease(&state, req.session_id.trim(), &request_id);
            fail(
                StatusCode::CONFLICT,
                "CREATION_SELECTION_BASE_CHANGED",
                "文档在提交前已经变化，请重新划选",
                false,
            )
        })?;
    release_creation_lease(&state, req.session_id.trim(), &request_id);
    Ok(Json(InlineEditResponse {
        schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
        request_id: request_id.clone(),
        status: "committed".to_string(),
        operation_fingerprint: operation_fingerprint.clone(),
        content: Some(result_content),
        replacement_markdown: Some(replacement),
        revision_no: Some(revision_no),
        patch: Some(patch),
        model_request: None,
        resume_state: None,
    }))
}

pub async fn cancel_creation_inline_edit(
    State(state): State<Arc<AppState>>,
    Json(req): Json<InlineEditCancelRequest>,
) -> Result<Json<InlineEditResponse>, InlineEditHttpError> {
    let run = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::get_by_request(conn, &req.request_id)
                .map_err(Into::into)
        })
        .map_err(|_| {
            inline_edit_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "读取选区运行失败",
                true,
                Some(&req.request_id),
            )
        })?
        .ok_or_else(|| {
            inline_edit_error(
                StatusCode::NOT_FOUND,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "选区运行不存在",
                false,
                Some(&req.request_id),
            )
        })?;
    if run.session_id != req.session_id {
        return Err(inline_edit_error(
            StatusCode::CONFLICT,
            "CREATION_INLINE_EDIT_IDEMPOTENCY_CONFLICT",
            "选区运行不属于当前会话",
            false,
            Some(&req.request_id),
        ));
    }
    if let Some(response) = inline_replay_response(run.clone()) {
        return Err(inline_edit_error(
            StatusCode::CONFLICT,
            "CREATION_INLINE_EDIT_COMMIT_IN_PROGRESS",
            "修改已经保存，请同步最新文档",
            false,
            Some(&response.request_id),
        ));
    }
    let cancelled = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::cancel_if_precommit(conn, &req.request_id)
                .map_err(Into::into)
        })
        .unwrap_or(false);
    if !cancelled {
        return Err(inline_edit_error(
            StatusCode::CONFLICT,
            "CREATION_INLINE_EDIT_COMMIT_IN_PROGRESS",
            "修改正在提交，请同步最新文档",
            false,
            Some(&req.request_id),
        ));
    }
    release_creation_lease(&state, &req.session_id, &req.request_id);
    Ok(Json(InlineEditResponse {
        schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
        request_id: req.request_id,
        status: "cancelled".to_string(),
        operation_fingerprint: run.operation_fingerprint,
        content: None,
        replacement_markdown: None,
        revision_no: None,
        patch: None,
        model_request: None,
        resume_state: None,
    }))
}

pub async fn undo_creation_inline_edit(
    State(state): State<Arc<AppState>>,
    Json(req): Json<InlineEditUndoRequest>,
) -> Result<Json<InlineEditResponse>, InlineEditHttpError> {
    let run = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::get_by_request(conn, &req.request_id)
                .map_err(Into::into)
        })
        .map_err(|_| {
            inline_edit_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "读取选区运行失败",
                true,
                Some(&req.request_id),
            )
        })?
        .ok_or_else(|| {
            inline_edit_error(
                StatusCode::NOT_FOUND,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "没有可撤销的选区修改",
                false,
                Some(&req.request_id),
            )
        })?;
    if run.session_id != req.session_id || run.history_id != req.history_id {
        return Err(inline_edit_error(
            StatusCode::CONFLICT,
            "CREATION_INLINE_EDIT_IDEMPOTENCY_CONFLICT",
            "撤销目标与当前会话不一致",
            false,
            Some(&req.request_id),
        ));
    }
    let history = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_history::get_by_id(conn, req.history_id)
                .map_err(Into::into)
        })
        .map_err(|_| {
            inline_edit_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "读取创作历史失败",
                true,
                Some(&req.request_id),
            )
        })?
        .ok_or_else(|| {
            inline_edit_error(
                StatusCode::NOT_FOUND,
                "CREATION_INLINE_EDIT_UNAVAILABLE",
                "创作文档不存在",
                false,
                Some(&req.request_id),
            )
        })?;
    if sha256_hex(&history.generated_content) != req.expected_result_hash {
        return Err(inline_edit_error(
            StatusCode::CONFLICT,
            "CREATION_SELECTION_BASE_CHANGED",
            "文档已经再次变化，不能撤销旧修改",
            false,
            Some(&req.request_id),
        ));
    }
    let base_hash = sha256_hex(&run.base_content);
    let patch = serde_json::json!({
        "operation": "undo_inline_edit",
        "base_hash": req.expected_result_hash,
        "result_hash": base_hash,
        "preserved_untouched": true,
        "selection": {},
        "replacement": {},
        "prefix_hash": sha256_hex(""),
        "suffix_hash": sha256_hex(""),
        "changes": [{
            "change_type": "modified",
            "section_title": "所选内容",
            "summary": "撤销本次选区修改"
        }],
        "change_count": 1,
        "summary": "已撤销本次选区修改"
    });
    let now = chrono::Utc::now().timestamp_millis();
    let conversation_json = append_history_json_item(
        history.conversation_json.as_deref(),
        serde_json::json!({
            "id": format!("inline-undo-{now}"),
            "role": "user",
            "content": "撤销本次选区修改",
            "createdAt": now,
        }),
    );
    let trace_json = append_history_json_item(
        history.agent_trace_json.as_deref(),
        serde_json::json!({
            "schema_version": INLINE_EDIT_SCHEMA_VERSION,
            "type": "document.patch.applied",
            "status": "completed",
            "summary": "已撤销本次选区修改",
            "data": {"patch": patch},
        }),
    );
    let patch_json = serde_json::to_string(&patch).unwrap_or_else(|_| "{}".to_string());
    let (content, revision_no) = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_inline_edit::undo_committed(
                conn,
                &req.request_id,
                &req.expected_result_hash,
                &conversation_json,
                &trace_json,
                &patch_json,
            )
            .map_err(Into::into)
        })
        .map_err(|_| {
            inline_edit_error(
                StatusCode::CONFLICT,
                "CREATION_SELECTION_BASE_CHANGED",
                "文档已经再次变化，不能撤销旧修改",
                false,
                Some(&req.request_id),
            )
        })?;
    Ok(Json(InlineEditResponse {
        schema_version: INLINE_EDIT_SCHEMA_VERSION.to_string(),
        request_id: req.request_id,
        status: "committed".to_string(),
        operation_fingerprint: run.operation_fingerprint,
        content: Some(content),
        replacement_markdown: None,
        revision_no: Some(revision_no),
        patch: Some(patch),
        model_request: None,
        resume_state: None,
    }))
}

pub async fn generate_document(
    State(state): State<Arc<AppState>>,
    Json(mut req): Json<GenerateRequest>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, (StatusCode, String)> {
    info!(
        "创作请求已接收: prompt_length={}",
        req.user_prompt.chars().count()
    );
    enrich_creation_model_from_preferences(&state, &mut req);
    let enabled_tools = normalize_creation_tool_ids(req.enabled_tools.clone());

    // 1. 查询文档模板
    let templates = state.storage.get_document_templates(Some(5)).map_err(
        |e: crate::storage::error::StorageError| {
            error!("查询文档模板失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        },
    )?;

    let design_templates: Vec<serde_json::Value> = templates
        .into_iter()
        .map(|t| {
            serde_json::json!({
                "title": t.title,
                "doc_type": t.doc_type,
                "sections_json": t.sections_json,
                "style_phrases": t.style_phrases,
            })
        })
        .collect();

    // 2. 构建时间线上下文（简化版）
    let timeline_context = if !req.timeline_ids.is_empty() {
        Some(format!("时间线 IDs: {:?}", req.timeline_ids))
    } else {
        None
    };

    // 3. 构建采集记录上下文（简化版）
    let capture_context = if !req.capture_ids.is_empty() {
        Some(format!("采集记录 IDs: {:?}", req.capture_ids))
    } else {
        None
    };

    // 4. 调用 ai-sidecar creation 服务
    let payload = CreationPayload {
        user_prompt: req.user_prompt,
        design_templates,
        timeline_context,
        capture_context,
        doc_type: req.doc_type,
        audience: req.audience,
        output_format: req.output_format,
        inherit_format: req.inherit_format,
        enable_rag: req.enable_rag,
        enable_web_search: req.enable_web_search,
        enable_image_generation: req.enable_image_generation,
        browser_extension_enabled: req.browser_extension_enabled,
        enabled_tools,
        content_weight: req.content_weight,
        quality_weight: req.quality_weight,
        completeness_weight: req.completeness_weight,
        usage_weight: req.usage_weight,
        format_weight: req.format_weight,
        freshness_weight: req.freshness_weight,
        max_references: req.max_references.clamp(1, 30),
        data_search_limit: req.data_search_limit.clamp(1, 50),
        creation_model: req.creation_model,
        creation_api_key: req.creation_api_key,
        creation_base_url: req.creation_base_url,
    };

    let client = creation_sidecar_client();
    let response = client
        .post(format!("{}/creation/generate", state.creation_sidecar_url))
        .json(&payload)
        .send()
        .await
        .map_err(|e| {
            error!("调用 ai-sidecar 失败: {}", e);
            (StatusCode::BAD_GATEWAY, format!("AI 服务不可用: {}", e))
        })?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        error!("ai-sidecar 返回错误: {} - {}", status, body);
        return Err((StatusCode::BAD_GATEWAY, format!("AI 服务错误: {}", body)));
    }

    // 5. 转发 SSE 流
    let stream = async_stream::stream! {
        let mut bytes_stream = response.bytes_stream();
        use futures::StreamExt;
        let mut buffer = Vec::new();

        while let Some(chunk) = bytes_stream.next().await {
            match chunk {
                Ok(bytes) => {
                    for content in append_sse_chunk(&mut buffer, &bytes) {
                        yield Ok(Event::default().data(content));
                    }
                }
                Err(e) => {
                    error!("流式读取错误: {}", e);
                    let payload = serde_json::json!({ "error": format!("AI 流式响应中断: {}", e) }).to_string();
                    yield Ok(Event::default().data(payload));
                    break;
                }
            }
        }
        if let Some(content) = take_sse_tail(&mut buffer) {
            yield Ok(Event::default().data(content));
        }
    };

    Ok(Sse::new(stream).keep_alive(
        axum::response::sse::KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("keep-alive"),
    ))
}

#[derive(Debug, Deserialize)]
pub struct UndoOperationRequest { pub session_id: String, pub operation_id: String }

pub async fn undo_creation_operation(State(state): State<Arc<AppState>>, Json(req): Json<UndoOperationRequest>)
    -> Result<Json<serde_json::Value>, (StatusCode,String)> {
    let content = state.storage.with_conn(|conn| {
        crate::storage::repo::creation_operation::undo(conn,&req.session_id,&req.operation_id).map_err(Into::into)
    }).map_err(|_| (StatusCode::CONFLICT,"CREATION_BASE_CHANGED".to_string()))?;
    Ok(Json(serde_json::json!({"content":content,"status":"undone"})))
}

pub async fn run_creation_agent(
    State(state): State<Arc<AppState>>,
    Json(mut req): Json<AgentRunRequest>,
) -> Result<Sse<futures::stream::BoxStream<'static, Result<Event, Infallible>>>, (StatusCode, String)> {
    if !matches!(req.execution_origin.as_str(), "interactive" | "scheduled_task") {
        return Err((
            StatusCode::BAD_REQUEST,
            "execution_origin 只支持 interactive 或 scheduled_task".to_string(),
        ));
    }
    if !matches!(req.model_mode.as_str(), "local" | "external") {
        return Err((
            StatusCode::BAD_REQUEST,
            "model_mode 只支持 local 或 external".to_string(),
        ));
    }
    if !matches!(req.creation_mode.as_str(), "direct" | "brainstorm") {
        return Err((
            StatusCode::BAD_REQUEST,
            "creation_mode 只支持 direct 或 brainstorm".to_string(),
        ));
    }
    info!(
        "创作 Agent 请求已接收: mode={}, prompt_length={}",
        req.model_mode,
        req.generation.user_prompt.chars().count()
    );
    // Local uses the sidecar's active model; external uses model.request via the
    // brand gateway. Legacy BYOK preferences must never override either mode.
    req.generation.creation_model = None;
    req.generation.creation_api_key = None;
    req.generation.creation_base_url = None;

    let stored_context = if let Some(session_id) = req.session_id.as_deref() {
        state
            .storage
            .with_conn(|conn| {
                crate::storage::repo::creation_history::get_session_context(conn, session_id)
                    .map_err(Into::into)
            })
            .map_err(|e| {
                error!("恢复创作会话上下文失败: {}", e);
                (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
            })?
    } else {
        None
    };
    if let Some(ref context) = stored_context {
        if req.current_document.trim().is_empty() {
            req.current_document = context.latest.generated_content.clone();
        }
        if req
            .root_request
            .as_deref()
            .map(str::trim)
            .unwrap_or_default()
            .is_empty()
        {
            req.root_request = Some(context.root_request.clone());
        }
        let stored_conversation = context
            .latest
            .conversation_json
            .as_deref()
            .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
            .unwrap_or_default();
        req.conversation = merge_creation_conversation(
            stored_conversation,
            req.conversation,
            req.root_request.as_deref(),
        );
    }
    if req
        .root_request
        .as_deref()
        .map(str::trim)
        .unwrap_or_default()
        .is_empty()
    {
        req.root_request = conversation_root_request(&req.conversation)
            .or_else(|| Some(req.generation.user_prompt.clone()));
    }
    if req
        .run_id
        .as_deref()
        .map(str::trim)
        .unwrap_or_default()
        .is_empty()
    {
        req.run_id = Some(format!("run-{}", uuid::Uuid::new_v4()));
    }

    let mut operation_context = None;
    let mut resume_checkpoint = None;
    let mut fresh_input_attempt = false;
    let mut durable_operation_id = None;
    if let (Some(session), Some(instruction_id), Some(context)) =
        (req.session_id.as_deref(), req.instruction_id.as_deref(), stored_context.as_ref()) {
        let operation_id = req.resume_operation_id.clone().unwrap_or_else(|| {
            format!("operation-{}", sha256_hex(&format!("{}:{}", session, instruction_id)))
        });
        let operation = state.storage.with_conn(|conn| {
            if req.resume_operation_id.is_none() {
                crate::storage::repo::creation_operation::seed_legacy_pending(conn, session, instruction_id, &context.latest)?;
                crate::storage::repo::creation_operation::begin(conn, session, &operation_id,
                    instruction_id, &req.generation.user_prompt, &context.latest)?;
            }
            crate::storage::repo::creation_operation::get_for_resume(conn,session,&operation_id).map_err(Into::into)
        }).map_err(|err| {
            if matches!(err, crate::storage::error::StorageError::Sqlite(rusqlite::Error::InvalidQuery)) {
                return (StatusCode::CONFLICT,"创作指令状态冲突，请重新打开会话后重试".to_string());
            }
            // Storage failures are server errors, not invalid user instructions.
            // Do not expose SQL or user content through the response.
            error!("创作操作持久化失败: {}", err);
            (StatusCode::INTERNAL_SERVER_ERROR,"创作记录写入失败，请重启应用完成数据升级后重试".to_string())
        })?
          .ok_or((StatusCode::NOT_FOUND,"CREATION_RESUME_MISSING".to_string()))?;
        if let Some(result) = operation.result.filter(|_| operation.status == "completed") {
            let events = futures::stream::iter(vec![Ok(Event::default().data(result.to_string()))]);
            return Ok(Sse::new(Box::pin(events) as futures::stream::BoxStream<'static, Result<Event, Infallible>>));
        }
        if !crate::storage::repo::creation_history::matches_document_base(&context.latest, session, operation.base_revision, &operation.base_document) {
            return Err((StatusCode::CONFLICT,"CREATION_BASE_CHANGED".to_string()));
        }
        let pending = state.storage.with_conn(|conn| {
            crate::storage::repo::creation_operation::pending(conn,session,&operation_id).map_err(Into::into)
        }).map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR,"读取未完成操作失败".to_string()))?;
        let undo_candidates = state.storage.with_conn(|conn| {
            crate::storage::repo::creation_operation::undo_candidates(conn,session,operation.base_revision).map_err(Into::into)
        }).map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR,"读取撤销记录失败".to_string()))?;
        operation_context = Some(serde_json::json!({"operation_id":operation_id, "instruction_id": instruction_id,
            "pending_operations":pending,"undo_candidates":undo_candidates,"base_revision":operation.base_revision}));
        let changed_inputs = operation.checkpoint.as_ref()
            .is_some_and(|checkpoint| !creation_checkpoint_inputs_match(&req, checkpoint));
        if let Some(checkpoint) = operation.checkpoint.filter(|_| !changed_inputs) {
            req.run_id = checkpoint["run_id"].as_str().map(str::to_string);
            if req.model_result.is_some() {
                let expected_request_id = checkpoint["pending_model_step"]["request_id"].as_str();
                if expected_request_id.is_none() || req.model_request_id.as_deref() != expected_request_id {
                    return Err((StatusCode::CONFLICT,"CREATION_MODEL_RESULT_STALE".to_string()));
                }
                req.resume_state = Some(checkpoint);
            } else {
                req.resume_state = None;
                req.run_id = Some(format!("run-{}", uuid::Uuid::new_v4()));
                resume_checkpoint = Some(checkpoint);
            }
        } else if changed_inputs {
            // The user revised source inputs without changing the instruction
            // or document base. Begin a fresh attempt instead of replaying an
            // old evidence/plan snapshot or accepting its late model result.
            req.resume_state = None;
            fresh_input_attempt = true;
            req.model_result = None;
            req.model_request_id = None;
            req.run_id = Some(format!("run-{}", uuid::Uuid::new_v4()));
            if req.resume_operation_id.is_some() {
                req.generation.user_prompt = operation.instruction;
            }
        } else if req.model_result.is_some() {
            return Err((StatusCode::CONFLICT,"CREATION_MODEL_RESULT_STALE".to_string()));
        } else if req.resume_operation_id.is_some() {
            // An operation persisted before its first model call can still resume its original instruction.
            req.generation.user_prompt = operation.instruction;
        }
        req.current_document = operation.base_document;
        durable_operation_id = Some(operation_id);
    }

    let templates = state.storage.get_document_templates(Some(5)).map_err(|e| {
        error!("查询文档模板失败: {}", e);
        (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
    })?;
    let design_templates = templates
        .into_iter()
        .map(|item| {
            serde_json::json!({
                "title": item.title,
                "doc_type": item.doc_type,
                "sections_json": item.sections_json,
                "style_phrases": item.style_phrases,
            })
        })
        .collect();
    let timeline_context = (!req.generation.timeline_ids.is_empty())
        .then(|| format!("时间线 IDs: {:?}", req.generation.timeline_ids));
    let capture_context = (!req.generation.capture_ids.is_empty())
        .then(|| format!("采集记录 IDs: {:?}", req.generation.capture_ids));
    let lease_session_id = req
        .session_id
        .clone()
        .filter(|value| !value.trim().is_empty());
    let lease_owner = format!("phase-{}", uuid::Uuid::new_v4());
    if let Some(session_id) = lease_session_id.as_deref() {
        if !acquire_creation_lease(&state, session_id, &lease_owner) {
            return Err((
                StatusCode::CONFLICT,
                "当前创作会话已有操作正在运行".to_string(),
            ));
        }
    }

    if let (Some(session), Some(operation_id)) = (lease_session_id.as_deref(), durable_operation_id.as_deref()) {
        if state.storage.with_conn(|conn| crate::storage::repo::creation_operation::restart_attempt(conn,session,operation_id,resume_checkpoint.is_some() || fresh_input_attempt).map_err(Into::into)).is_err() {
            release_creation_lease(&state,session,&lease_owner);
            return Err((StatusCode::INTERNAL_SERVER_ERROR,"恢复操作失败".to_string()));
        }
    }
    let generation = req.generation;
    let enabled_tools = normalize_creation_tool_ids(generation.enabled_tools.clone());
    let payload = AgentRunPayload {
        creation: CreationPayload {
            user_prompt: generation.user_prompt,
            design_templates,
            timeline_context,
            capture_context,
            doc_type: generation.doc_type,
            audience: generation.audience,
            output_format: generation.output_format,
            inherit_format: generation.inherit_format,
            enable_rag: generation.enable_rag,
            enable_web_search: generation.enable_web_search,
            enable_image_generation: generation.enable_image_generation,
            browser_extension_enabled: generation.browser_extension_enabled,
            enabled_tools,
            content_weight: generation.content_weight,
            quality_weight: generation.quality_weight,
            completeness_weight: generation.completeness_weight,
            usage_weight: generation.usage_weight,
            format_weight: generation.format_weight,
            freshness_weight: generation.freshness_weight,
            max_references: generation.max_references.clamp(1, 30),
            data_search_limit: generation.data_search_limit.clamp(1, 50),
            creation_model: generation.creation_model,
            creation_api_key: generation.creation_api_key,
            creation_base_url: generation.creation_base_url,
        },
        session_id: req.session_id,
        run_id: req.run_id,
        root_request: req.root_request,
        current_document: req.current_document,
        conversation: req.conversation,
        selected_skills: req.selected_skills,
        available_skills: req.available_skills,
        explicit_skill_ids: req.explicit_skill_ids,
        model_mode: req.model_mode,
        confirmed: req.confirmed,
        resume_state: req.resume_state,
        model_result: req.model_result,
        creation_mode: req.creation_mode,
        creation_brief: req.creation_brief,
        execution_origin: req.execution_origin,
        operation_context,
        resume_checkpoint,
    };

    let response = creation_sidecar_client()
        .post(format!(
            "{}/creation/agent/run",
            state.creation_sidecar_url.trim_end_matches('/')
        ))
        .json(&payload)
        .send()
        .await
        .map_err(|e| {
            if let Some(session_id) = lease_session_id.as_deref() {
                release_creation_lease(&state, session_id, &lease_owner);
            }
            error!("调用创作 Agent sidecar 失败: {}", e);
            (
                StatusCode::BAD_GATEWAY,
                format!("创作 Agent 服务不可用: {}", e),
            )
        })?;
    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        error!("创作 Agent sidecar 返回错误: {} - {}", status, body);
        if let Some(session_id) = lease_session_id.as_deref() {
            release_creation_lease(&state, session_id, &lease_owner);
        }
        return Err((
            StatusCode::BAD_GATEWAY,
            format!("创作 Agent 服务错误: {}", body),
        ));
    }

    let durable_session_id = lease_session_id.clone();
    let durable_lease_owner = lease_owner.clone();
    let lease_guard = CreationLeaseGuard {
        state: state.clone(),
        session_id: lease_session_id,
        operation_id: durable_operation_id.clone(),
        owner: lease_owner,
    };
    let stream = async_stream::stream! {
        let _lease_guard = lease_guard;
        let mut bytes_stream = response.bytes_stream();
        use futures::StreamExt;
        let mut buffer = Vec::new();
        while let Some(chunk) = bytes_stream.next().await {
            match chunk {
                Ok(bytes) => {
                    if let Some(session) = durable_session_id.as_deref() {
                        // Sidecar emits a comment heartbeat during long local searches.
                        // Renew the in-memory lease on every received chunk so stale-run
                        // reconciliation cannot misclassify a genuinely active long run.
                        touch_creation_lease(&state, session, &durable_lease_owner);
                    }
                    for content in append_sse_chunk(&mut buffer, &bytes) {
                        let mut event = serde_json::from_str::<serde_json::Value>(&content).unwrap_or(serde_json::Value::Null);
                        if let (Some(session),Some(operation_id)) = (durable_session_id.as_deref(),durable_operation_id.as_deref()) {
                            if state.storage.with_conn(|conn| {
                                crate::storage::repo::creation_operation::record_event(conn,session,operation_id,&mut event).map_err(Into::into)
                            }).is_err() {
                                let failure = serde_json::json!({"type":"run.failed","status":"failed",
                                    "session_id":session,"run_id":event["run_id"],
                                    "summary":"操作状态保存失败或文档版本已变化，请重新载入后重试",
                                    "data":{"error_code":"CREATION_BASE_CHANGED","retryable":false}});
                                yield Ok(Event::default().data(failure.to_string()));
                                return;
                            }
                            if event["type"] == "operation.checkpoint" { continue; }
                        }
                        yield Ok(Event::default().data(if event.is_null() { content } else { event.to_string() }));
                    }
                }
                Err(e) => {
                    error!("创作 Agent 流式读取错误: {}", e);
                    let payload = serde_json::json!({
                        "schema_version": "creation.agent.v1",
                        "type": "run.failed",
                        "status": "failed",
                        "summary": format!("创作 Agent 流式响应中断: {}", e)
                    }).to_string();
                    yield Ok(Event::default().data(payload));
                    break;
                }
            }
        }
        if let Some(content) = take_sse_tail(&mut buffer) {
            let mut event = serde_json::from_str::<serde_json::Value>(&content).unwrap_or(serde_json::Value::Null);
            if let (Some(session),Some(operation_id)) = (durable_session_id.as_deref(),durable_operation_id.as_deref()) {
                if state.storage.with_conn(|conn| crate::storage::repo::creation_operation::record_event(conn,session,operation_id,&mut event).map_err(Into::into)).is_err() {
                    yield Ok(Event::default().data(serde_json::json!({"type":"run.failed","status":"failed","summary":"操作提交失败","data":{"error_code":"CREATION_BASE_CHANGED"}}).to_string()));
                    return;
                }
                if event["type"] == "operation.checkpoint" { return; }
            }
            yield Ok(Event::default().data(if event.is_null() {content} else {event.to_string()}));
        }
    };

    Ok(Sse::new(Box::pin(stream) as futures::stream::BoxStream<'static, Result<Event, Infallible>>).keep_alive(
        axum::response::sse::KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("keep-alive"),
    ))
}

fn answer_summary(question: &BrainstormQuestion, answer: &BrainstormAnswer) -> String {
    let mut values = answer
        .selected_option_ids
        .iter()
        .filter_map(|id| question.options.iter().find(|item| &item.id == id))
        .map(|item| item.label.clone())
        .collect::<Vec<_>>();
    if !answer.custom_text.trim().is_empty() {
        values.push(answer.custom_text.trim().to_string());
    }
    values.join("；")
}

fn validate_brainstorm_answer(question: &BrainstormQuestion, answer: &BrainstormAnswer) -> bool {
    let selected_valid = answer
        .selected_option_ids
        .iter()
        .all(|id| question.options.iter().any(|option| option.id == *id));
    if !selected_valid
        || answer
            .selected_option_ids
            .iter()
            .collect::<std::collections::HashSet<_>>()
            .len()
            != answer.selected_option_ids.len()
    {
        return false;
    }
    let has_selected_options = !answer.selected_option_ids.is_empty();
    let has_custom_text = !answer.custom_text.trim().is_empty();
    if has_selected_options && has_custom_text {
        return false;
    }
    match question.question_type.as_str() {
        "single_choice" => {
            answer.selected_option_ids.len() == 1
                || (!has_selected_options && question.allow_custom && has_custom_text)
        }
        "multi_choice" => has_selected_options || (question.allow_custom && has_custom_text),
        // 仅用于读取旧版已回答记录，不会作为新的当前问题返回。
        "confirm_inference" => !has_selected_options && has_custom_text,
        _ => false,
    }
}

fn is_enumerated_brainstorm_question(question: &BrainstormQuestion) -> bool {
    if !matches!(
        question.question_type.as_str(),
        "single_choice" | "multi_choice"
    ) || !question.allow_custom
        || !(2..=5).contains(&question.options.len())
        || question.id.trim().is_empty()
        || question.prompt.trim().is_empty()
        || question.why_now.trim().is_empty()
    {
        return false;
    }
    let mut option_ids = std::collections::HashSet::new();
    let mut recommended_count = 0;
    for option in &question.options {
        if option.id.trim().is_empty()
            || option.label.trim().is_empty()
            || option.description.trim().is_empty()
            || !option_ids.insert(option.id.as_str())
        {
            return false;
        }
        if option.recommended {
            recommended_count += 1;
        }
    }
    recommended_count == 1
}

fn brainstorm_effective_summary(
    state: &BrainstormStoredState,
    turn: &BrainstormStoredTurn,
) -> String {
    if turn.answer.source == "user_excluded" {
        return format!("该方向不重要，禁止展开或写入文档：{}", turn.question.prompt);
    }
    state
        .brief_edits
        .get(&turn.question.id)
        .cloned()
        .unwrap_or_else(|| answer_summary(&turn.question, &turn.answer))
}

fn brainstorm_question_is_current(state: &BrainstormStoredState, question: &BrainstormQuestion) -> bool {
    let mut question = question;
    let mut visited = std::collections::HashSet::new();
    while visited.insert(question.id.as_str()) {
        let Some(parent) = question.parent_question_id.as_deref().and_then(|id| {
            state.turns.iter().find(|turn| turn.question.id == id)
        }) else {
            return true;
        };
        if state.brief_edits.contains_key(&parent.question.id) {
            if !state.branch_parent_revisions.get(&question.id)
                .zip(state.user_input_revisions.get(&parent.question.id))
                .is_some_and(|(basis, current)| basis >= current)
            {
                return false;
            }
        } else if parent.answer.source != "user" {
            return false;
        }
        question = &parent.question;
    }
    false
}

fn archive_superseded_brainstorm_turns(state: &mut BrainstormStoredState) -> Vec<String> {
    let invalidated = state.turns.iter()
        .filter(|turn| !brainstorm_question_is_current(state, &turn.question))
        .map(|turn| turn.question.id.clone()).collect::<Vec<_>>();
    state.pending_extensions.retain(|plan| {
        !invalidated.contains(&plan.parent_question_id)
            && state.turns.iter().any(|turn| turn.question.id == plan.parent_question_id)
            && state.user_input_revisions.get(&plan.parent_question_id).copied().unwrap_or(0) == plan.parent_revision
    });
    let turns = std::mem::take(&mut state.turns);
    for turn in turns {
        if invalidated.contains(&turn.question.id) {
            state.brief_edits.remove(&turn.question.id);
            state.user_input_revisions.remove(&turn.question.id);
            state.archived_questions.push(BrainstormArchivedQuestion {
                question: turn.question, answer: Some(turn.answer),
            });
        } else {
            state.turns.push(turn);
        }
    }
    invalidated
}

fn brainstorm_effective_open_flags(state: &BrainstormStoredState) -> Vec<String> {
    state
        .brief_edits
        .get("open_flags")
        .map(|value| {
            value
                .lines()
                .filter(|line| !line.trim().is_empty())
                .map(String::from)
                .collect()
        })
        .unwrap_or_else(|| state.open_flags.clone())
}

fn brainstorm_brief(state: &BrainstormStoredState) -> String {
    let mut lines = vec![
        "# 创作简报".to_string(),
        String::new(),
        format!(
            "**原始需求：** {}",
            state
                .brief_edits
                .get("root_request")
                .unwrap_or(&state.root_request)
        ),
    ];
    if !state.memory_brief.is_empty() {
        lines.push(format!("\n## 历史记忆参考（当前用户修订优先）\n{}", state.memory_brief));
    }
    for turn in state.turns.iter().filter(|turn| brainstorm_question_is_current(state, &turn.question)) {
        if turn.answer.source == "user_excluded" {
            lines.push(format!("\n- **排除约束（不得写入正文，也不得列为待补充或未展开方向）：** {}", turn.question.prompt));
            continue;
        }
        lines.push(String::new());
        lines.push(format!("## {}", turn.question.dimension));
        let label = if turn.answer.source == "agent_assumption"
            && !state.brief_edits.contains_key(&turn.question.id)
        {
            "合理假设"
        } else {
            "已确认"
        };
        lines.push(format!("- **问题：** {}", turn.question.prompt));
        lines.push(format!(
            "- **{}：** {}",
            label,
            state
                .brief_edits
                .get(&turn.question.id)
                .cloned()
                .unwrap_or_else(|| answer_summary(&turn.question, &turn.answer))
        ));
    }
    let mut open = state.open_flags.clone();
    if let Some(question) = state.current_question.as_ref().filter(|question| brainstorm_question_is_current(state, question)) {
        if !open.iter().any(|item| item == &question.prompt) {
            open.insert(0, question.prompt.clone());
        }
    }
    if let Some(edited) = state.brief_edits.get("open_flags") {
        open = edited.lines().map(String::from).collect();
    }
    if !open.is_empty() {
        lines.push(String::new());
        lines.push("## 待决定".to_string());
        for item in open {
            lines.push(format!("- {}", item));
        }
    }
    lines.join("\n")
}

fn brainstorm_response(
    session_id: &str,
    phase: &str,
    revision: i64,
    state: &BrainstormStoredState,
) -> BrainstormTurnResponse {
    let decisions = state
        .turns
        .iter()
        .filter(|turn| brainstorm_question_is_current(state, &turn.question))
        .map(|turn| BrainstormDecision {
            question_id: turn.question.id.clone(),
            dimension_id: turn.question.dimension_id.clone(),
            dimension: turn.question.dimension.clone(),
            summary: brainstorm_effective_summary(state, turn),
            user_inputs: brainstorm_user_inputs(state, turn),
            source: if turn.answer.source != "user_excluded" && state.brief_edits.contains_key(&turn.question.id) {
                "user".to_string()
            } else {
                turn.answer.source.clone()
            },
        })
        .collect::<Vec<_>>();
    let history = state
        .turns
        .iter()
        .map(|turn| BrainstormTurnHistoryItem {
            question: turn.question.clone(),
            answer: turn.answer.clone(),
        })
        .collect::<Vec<_>>();
    BrainstormTurnResponse {
        archived_questions: state.archived_questions.clone(),
        root_request: state.root_request.clone(),
        brief_edits: state.brief_edits.clone(),
        user_input_revisions: state.user_input_revisions.clone(),
        session_id: session_id.to_string(),
        phase: phase.to_string(),
        revision,
        current_question: state.current_question.clone(),
        brief_markdown: brainstorm_brief(state),
        answered_count: state.turns.len(),
        depth: state.turns.len(),
        can_continue_brainstorm: matches!(phase, "ready" | "choosing_direction")
            && !state.continuation_directions.is_empty(),
        open_flags: brainstorm_effective_open_flags(state),
        readiness_reason: state.readiness_reason.clone(),
        continuation_directions: state.continuation_directions.clone(),
        invalidated_question_ids: state.invalidated_question_ids.clone(),
        decisions,
        history,
    }
}

#[derive(Debug, Serialize)]
struct DynamicBrainstormRequest {
    question_batch_limit: usize,
    extension_goal: String,
    sibling_question_context: Vec<String>,
    prefetch: bool,
    root_request: String,
    decisions: Vec<serde_json::Value>,
    brief_markdown: String,
    brief_edits: std::collections::BTreeMap<String, String>,
    user_input_revisions: std::collections::BTreeMap<String, i64>,
    selected_skills: Vec<serde_json::Value>,
    force_continue: bool,
    suggest_directions: bool,
    focus_hint: String,
    focus_hint_source: String,
    exploration_stage: String,
    creation_model: Option<String>,
    creation_api_key: Option<String>,
    creation_base_url: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct DynamicBrainstormResult {
    #[serde(default)]
    sibling_question_goals: Vec<String>,
    #[serde(skip)]
    consumed_extension_id: Option<String>,
    #[serde(default)]
    prefetch_safe_option_ids: Vec<String>,
    #[serde(default)]
    memory_brief: String,
    status: String,
    #[serde(default)]
    readiness_reason: String,
    #[serde(default)]
    open_flags: Vec<String>,
    #[serde(default)]
    continuation_directions: Vec<BrainstormContinuationDirection>,
    #[serde(default)]
    question: Option<BrainstormQuestion>,
}

fn brainstorm_decision_context(state: &BrainstormStoredState) -> Vec<serde_json::Value> {
    state
        .turns
        .iter()
        .filter(|turn| brainstorm_question_is_current(state, &turn.question))
        .map(|turn| {
            let selected_options = turn
                .answer
                .selected_option_ids
                .iter()
                .filter_map(|id| turn.question.options.iter().find(|option| &option.id == id))
                .map(|option| {
                    serde_json::json!({
                        "id": option.id,
                        "label": option.label,
                        "tradeoff": option.description,
                    })
                })
                .collect::<Vec<_>>();
            serde_json::json!({
                "question_id": turn.question.id,
                "parent_question_id": turn.question.parent_question_id,
                "parent_option_id": turn.question.parent_option_id,
                "exploration_stage": brainstorm_exploration_stage(&turn.question),
                "dimension_id": turn.question.dimension_id,
                "dimension": turn.question.dimension,
                "question": turn.question.prompt,
                "answer": brainstorm_effective_summary(state, turn),
                "user_inputs": brainstorm_user_inputs(state, turn),
                "manually_edited": state.brief_edits.contains_key(&turn.question.id),
                "answer_source": if turn.answer.source != "user_excluded" && state.brief_edits.contains_key(&turn.question.id) { "user" } else { &turn.answer.source },
                "selected_options": if state.brief_edits.contains_key(&turn.question.id) { Vec::new() } else { selected_options },
            })
        })
        .collect()
}

fn brainstorm_user_inputs(state: &BrainstormStoredState, turn: &BrainstormStoredTurn) -> Vec<String> {
    if turn.answer.source == "user_excluded" {
        return Vec::new();
    }
    if let Some(edited) = state.brief_edits.get(&turn.question.id) {
        return vec![edited.clone()];
    }
    if turn.answer.source != "user" {
        return Vec::new();
    }
    let mut inputs = turn.answer.selected_option_ids.iter()
        .filter_map(|id| turn.question.options.iter().find(|option| &option.id == id))
        .map(|option| option.label.clone()).collect::<Vec<_>>();
    if !turn.answer.custom_text.trim().is_empty() {
        inputs.push(turn.answer.custom_text.trim().to_string());
    }
    inputs
}

fn initialize_brainstorm_input_revisions(stored: &mut BrainstormStoredState, revision: i64) {
    // Migrate once inside the next ordinary state write. An edited field with
    // unknown chronology is conservatively current; never refresh it on restore.
    stored.user_input_revisions.entry("root_request".to_string()).or_insert(
        if stored.brief_edits.contains_key("root_request") { revision } else { 0 }
    );
    for (index, turn) in stored.turns.iter().enumerate() {
        let inferred = if stored.brief_edits.contains_key(&turn.question.id) {
            revision
        } else {
            ((index + 1) as i64).min(revision)
        };
        stored.user_input_revisions.entry(turn.question.id.clone()).or_insert(inferred);
    }
    for key in stored.brief_edits.keys() {
        stored.user_input_revisions.entry(key.clone()).or_insert(revision);
    }
}

fn brainstorm_exploration_stage(question: &BrainstormQuestion) -> &str {
    question.exploration_stage.as_deref().unwrap_or("explore")
}

fn next_brainstorm_exploration_stage(question: &BrainstormQuestion) -> Option<&'static str> {
    match brainstorm_exploration_stage(question) {
        "explore" => Some("solutions"),
        "solutions" => Some("implementation"),
        "implementation" => Some("validation"),
        // A discussed validation plan ends this leaf, without claiming it was executed.
        "validation" => None,
        _ => None,
    }
}

struct PendingBrainstormBranch<'a> {
    question: &'a BrainstormQuestion,
    option: Option<&'a BrainstormOption>,
    focus: String,
    exploration_stage: &'static str,
    extension: Option<&'a BrainstormExtensionPlan>,
    depth: usize,
}

fn brainstorm_branch_focus(stored: &BrainstormStoredState, branch: &PendingBrainstormBranch<'_>) -> String {
    let mut path = vec![branch.focus.clone()];
    let mut question = branch.question;
    let mut visited = std::collections::HashSet::new();
    while visited.insert(question.id.as_str()) {
        let Some(parent) = question.parent_question_id.as_deref().and_then(|id| {
            stored.turns.iter().find(|turn| turn.question.id == id)
        }) else {
            break;
        };
        let input = if let Some(edited) = stored.brief_edits.get(&parent.question.id) {
            edited.clone()
        } else if parent.answer.source == "user" {
            question.parent_option_id.as_deref().and_then(|id| {
                parent.question.options.iter().find(|option| option.id == id
                    && parent.answer.selected_option_ids.contains(&option.id))
            }).map(|option| option.label.clone()).unwrap_or_else(|| parent.answer.custom_text.clone())
        } else {
            String::new()
        };
        if !input.trim().is_empty() {
            path.push(input.trim().to_string());
        }
        question = &parent.question;
    }
    path.reverse();
    format!("按层逐项展开，当前用户决定路径：{}。本题只讨论当前路径的指定阶段；同层其他已选方向分别讨论后，再进入下一层。", path.join(" → "))
}

// Traverse each root by breadth, preserving displayed option order. An explicit
// continuation root takes precedence over older roots without losing ancestry.
fn pending_brainstorm_branches(stored: &BrainstormStoredState) -> Vec<PendingBrainstormBranch<'_>> {
    let mut pending = Vec::new();
    let mut visited = std::collections::HashSet::new();
    for root in stored.turns.iter().rev().filter(|turn| {
        !stored.turns.iter().any(|parent| {
            turn.question.parent_question_id.as_deref() == Some(parent.question.id.as_str())
        })
    }) {
        let root_start = pending.len();
        let mut queue = std::collections::VecDeque::from([(root, 0)]);
        while let Some((turn, depth)) = queue.pop_front() {
            if !visited.insert(turn.question.id.as_str())
                || !brainstorm_question_is_current(stored, &turn.question)
                || turn.answer.source == "user_excluded" {
                continue;
            }
            let Some(exploration_stage) = next_brainstorm_exploration_stage(&turn.question) else { continue; };
            let mut branches = Vec::new();
            if let Some(edited) = stored.brief_edits.get(&turn.question.id) {
                if !edited.trim().is_empty() { branches.push((None, edited.trim().to_string())); }
            } else if turn.answer.source == "user" {
                for option in &turn.question.options {
                    if turn.answer.selected_option_ids.contains(&option.id) {
                        branches.push((Some(option), option.label.clone()));
                    }
                }
                if !turn.answer.custom_text.trim().is_empty() {
                    branches.push((None, turn.answer.custom_text.trim().to_string()));
                }
            }
            for (option, focus) in branches {
                let option_id = option.map(|option| option.id.as_str());
                let matches = |question: &BrainstormQuestion| {
                    question.parent_question_id.as_deref() == Some(turn.question.id.as_str())
                        && question.parent_option_id.as_deref() == option_id
                        && brainstorm_question_is_current(stored, question)
                };
                let children: Vec<_> = stored.turns.iter().filter(|child| matches(&child.question)).collect();
                // The visible unanswered question occupies its branch, but its
                // unknown answers must never create speculative descendants.
                if children.is_empty() && !stored.current_question.as_ref().is_some_and(matches) {
                    pending.push(PendingBrainstormBranch {question: &turn.question, option, focus: focus.clone(), exploration_stage, extension: None, depth});
                }
                for extension in stored.pending_extensions.iter().filter(|plan| {
                    plan.parent_question_id == turn.question.id
                        && plan.parent_option_id.as_deref() == option_id
                        && stored.user_input_revisions.get(&turn.question.id).copied().unwrap_or(0) == plan.parent_revision
                }) {
                    pending.push(PendingBrainstormBranch {
                        question: &turn.question, option, focus: focus.clone(), exploration_stage,
                        extension: Some(extension), depth,
                    });
                }
                queue.extend(children.into_iter().map(|child| (child, depth + 1)));
            }
        }
        // Rotate first questions across the entire layer before second/third
        // independent facets, including facets under different sibling parents.
        pending[root_start..].sort_by_key(|branch| (branch.depth,
            branch.extension.map(|plan| plan.ordinal).unwrap_or(0)));
    }
    pending
}

fn pending_brainstorm_branch(stored: &BrainstormStoredState) -> Option<PendingBrainstormBranch<'_>> {
    pending_brainstorm_branches(stored).into_iter().next()
}

fn apply_brainstorm_question_stage(
    question: &mut BrainstormQuestion,
    branch: Option<&PendingBrainstormBranch<'_>>,
) -> bool {
    let expected_stage = branch.map(|branch| branch.exploration_stage);
    let stage = question.exploration_stage.as_deref().or(expected_stage).unwrap_or("explore");
    if !matches!(stage, "explore" | "solutions" | "implementation" | "validation")
        || expected_stage.is_some_and(|expected| expected != stage)
    {
        return false;
    }
    // Omitted stage is the legacy Sidecar contract. Explicit mismatches above
    // must fail, rather than relabelling a diagnostic question as a solution.
    question.exploration_stage = Some(stage.to_string());
    question.parent_question_id = branch.map(|branch| branch.question.id.clone());
    question.parent_option_id = branch.and_then(|branch| branch.option.map(|option| option.id.clone()));
    true
}

fn brainstorm_log_identifier(value: &str) -> &str {
    let value = value.trim();
    if !value.is_empty()
        && value.len() <= 160
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b':')
        })
    {
        value
    } else {
        "invalid"
    }
}

fn log_brainstorm_stage(req: &BrainstormTurnRequest, stage: &'static str, started: Instant) {
    info!(
        session_id = brainstorm_log_identifier(&req.session_id),
        action = brainstorm_log_action(&req.action),
        stage,
        elapsed_ms = started.elapsed().as_millis() as u64,
        "creation brainstorm stage"
    );
}

fn brainstorm_log_action(action: &str) -> &str {
    match action {
        "start" | "edit_brief" | "answer" | "skip" | "exclude" | "reopen"
        | "revise_answer" | "finish" | "change_direction" | "continue_brainstorm"
        | "abandon" => action,
        _ => "invalid",
    }
}

struct BrainstormTurnTrace {
    session_id: String,
    action: String,
    started: Instant,
    finished: bool,
}

impl Drop for BrainstormTurnTrace {
    fn drop(&mut self) {
        if !self.finished {
            info!(
                session_id = self.session_id,
                action = self.action,
                stage = "cancelled",
                elapsed_ms = self.started.elapsed().as_millis() as u64,
                "creation brainstorm stage"
            );
        }
    }
}

async fn generate_uncached_brainstorm_step(
    state: &AppState,
    stored: &BrainstormStoredState,
    req: &BrainstormTurnRequest,
    force_continue: bool,
    suggest_directions: bool,
    focus_hint: &str,
    target_key: Option<&str>,
    prefetch: bool,
) -> Result<DynamicBrainstormResult, (StatusCode, Json<serde_json::Value>)> {
    with_brainstorm_session_guard(state, req.session_id.trim(), false, |_| Ok(()))?;
    // Filter the entire effective input snapshot, including manual fields and
    // permission revisions. Superseded answers remain recoverable in history,
    // but cannot influence the model while generating their replacements.
    let mut effective_stored = stored.clone();
    archive_superseded_brainstorm_turns(&mut effective_stored);
    let stored = &effective_stored;
    let started = Instant::now();
    log_brainstorm_stage(req, "sidecar_started", started);
    let branch = if !suggest_directions {
        let mut branches = pending_brainstorm_branches(stored).into_iter();
        if let Some(key) = target_key {
            branches.find(|branch| brainstorm_prefetch::branch_key(stored, req, branch) == key)
        } else { branches.next() }
    } else {
        None
    };
    if target_key.is_some() && branch.is_none() {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let branch_hint = branch.as_ref().map(|branch| brainstorm_branch_focus(stored, branch));
    let force_continue = force_continue || branch.is_some();
    let question_batch_limit = if branch.as_ref().is_some_and(|branch| branch.extension.is_none()) {
        std::env::var("MEMORYBREAD_BRAINSTORM_QUESTION_BATCH_LIMIT").ok()
            .and_then(|value| value.parse::<usize>().ok()).unwrap_or(3).clamp(1, 8)
    } else { 1 };
    let payload = DynamicBrainstormRequest {
        question_batch_limit,
        extension_goal: branch.as_ref().and_then(|branch| branch.extension)
            .map(|plan| plan.goal.clone()).unwrap_or_default(),
        sibling_question_context: branch.as_ref().map(|branch| {
            let option_id = branch.option.map(|option| option.id.as_str());
            let mut context: Vec<String> = stored.turns.iter().map(|turn| &turn.question)
                .chain(stored.current_question.iter()).filter(|question| {
                    question.parent_question_id.as_deref() == Some(branch.question.id.as_str())
                        && question.parent_option_id.as_deref() == option_id
                        && brainstorm_question_is_current(stored, question)
                }).map(|question| question.prompt.clone()).collect();
            context.extend(stored.pending_extensions.iter().filter(|plan| {
                plan.parent_question_id == branch.question.id
                    && plan.parent_option_id.as_deref() == option_id
                    && branch.extension.map(|current| &current.id) != Some(&plan.id)
            }).map(|plan| plan.goal.clone()));
            context
        }).unwrap_or_default(),
        prefetch,
        root_request: stored
            .brief_edits
            .get("root_request")
            .cloned()
            .unwrap_or_else(|| stored.root_request.clone()),
        decisions: brainstorm_decision_context(stored),
        brief_markdown: brainstorm_brief(stored),
        brief_edits: stored.brief_edits.clone(),
        user_input_revisions: stored.user_input_revisions.clone(),
        selected_skills: stored.selected_skills.clone(),
        force_continue,
        suggest_directions,
        focus_hint: branch_hint.unwrap_or_else(|| focus_hint.trim().to_string()),
        focus_hint_source: if branch.is_some() || req.continuation_direction_id != "__custom__" {
            "confirmed_selection".to_string()
        } else {
            "user".to_string()
        },
        exploration_stage: branch.as_ref().map(|branch| branch.exploration_stage).unwrap_or("").to_string(),
        creation_model: req.creation_model.clone(),
        creation_api_key: req.creation_api_key.clone(),
        creation_base_url: req.creation_base_url.clone(),
    };
    let response = creation_sidecar_client()
        .post(format!(
            "{}/creation/brainstorm/next",
            state.creation_sidecar_url.trim_end_matches('/')
        ))
        .timeout(Duration::from_secs(180))
        .json(&payload)
        .send()
        .await
        .map_err(|error| {
            let code = if error.is_timeout() {
                "BRAINSTORM_MODEL_TIMEOUT"
            } else {
                "BRAINSTORM_MODEL_UNAVAILABLE"
            };
            error!("动态脑暴调用失败: code={}", code);
            brainstorm_model_error(code)
        })?;
    if !response.status().is_success() {
        let status = response.status();
        let body = response.json::<serde_json::Value>().await.unwrap_or_default();
        let failure = brainstorm_sidecar_error(
            StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY),
            &body,
        );
        // Never log upstream messages: they may contain model output, URLs or credentials.
        error!("动态脑暴返回错误: status={}, code={}", status, failure.1.0["code"]);
        return Err(failure);
    }
    log_brainstorm_stage(req, "sidecar_succeeded", started);
    let mut result = response
        .json::<DynamicBrainstormResult>()
        .await
        .map_err(|_| {
            log_brainstorm_stage(req, "validation_failed", started);
            error!("动态脑暴响应解析失败: code=BRAINSTORM_MODEL_OUTPUT_INVALID");
            brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID")
        })?;
    let valid = match result.status.as_str() {
        "question" => {
            !suggest_directions
                && result
                    .question
                    .as_ref()
                    .is_some_and(is_enumerated_brainstorm_question)
        }
        "ready" => {
            result.question.is_none()
                && !force_continue
                && (2..=4).contains(&result.continuation_directions.len())
        }
        _ => false,
    };
    if !valid {
        log_brainstorm_stage(req, "validation_failed", started);
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    let mut goal_fingerprints = std::collections::HashSet::new();
    if result.sibling_question_goals.len() >= question_batch_limit
        || result.sibling_question_goals.iter().any(|goal| {
            let fingerprint = brainstorm_question_fingerprint(goal);
            goal.trim().is_empty() || goal.chars().count() > 80 || fingerprint.is_empty()
                || !goal_fingerprints.insert(fingerprint.clone())
                || stored.turns.iter().any(|turn| brainstorm_question_fingerprint(&turn.question.prompt) == fingerprint)
                || result.question.as_ref().is_some_and(|question| brainstorm_question_fingerprint(&question.prompt) == fingerprint)
                || branch.as_ref().is_some_and(|branch| stored.pending_extensions.iter().any(|plan| {
                    plan.parent_question_id == branch.question.id
                        && plan.parent_option_id.as_deref() == branch.option.map(|option| option.id.as_str())
                        && brainstorm_question_fingerprint(&plan.goal) == fingerprint
                }))
        })
    {
        return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
    }
    result.consumed_extension_id = branch.as_ref().and_then(|branch| branch.extension).map(|plan| plan.id.clone());
    if let Some(question) = result.question.as_mut() {
        if result.consumed_extension_id.is_some() || !result.sibling_question_goals.is_empty() {
            question.id = format!("extension_{}", uuid::Uuid::new_v4());
        }
        // Parent links are authoritative Core metadata, never model supplied.
        if !apply_brainstorm_question_stage(question, branch.as_ref()) {
            log_brainstorm_stage(req, "validation_failed", started);
            return Err(brainstorm_model_error("BRAINSTORM_MODEL_OUTPUT_INVALID"));
        }
    }
    Ok(result)
}

async fn generate_dynamic_brainstorm_step(
    state: &AppState, stored: &BrainstormStoredState, req: &BrainstormTurnRequest,
    force_continue: bool, suggest_directions: bool, focus_hint: &str,
) -> Result<DynamicBrainstormResult, (StatusCode, Json<serde_json::Value>)> {
    with_brainstorm_session_guard(state, req.session_id.trim(), false, |_| Ok(()))?;
    if !suggest_directions {
        if let Some(step) = brainstorm_prefetch::take(state, stored, req) {
            return Ok(step);
        }
    }
    // An incomplete speculative request must yield to the demanded P0 turn.
    brainstorm_prefetch::stop_running(state, req.session_id.trim());
    generate_uncached_brainstorm_step(state, stored, req, force_continue, suggest_directions, focus_hint, None, false).await
}

fn apply_dynamic_step(
    stored: &mut BrainstormStoredState,
    result: DynamicBrainstormResult,
) -> String {
    stored.current_extension_id = result.consumed_extension_id.clone();
    if let Some(question) = result.question.as_ref() {
        stored.prefetch_safe_options.insert(question.id.clone(), result.prefetch_safe_option_ids.clone());
    }
    stored.open_flags = result.open_flags;
    stored.readiness_reason = result.readiness_reason;
    stored.memory_brief = result.memory_brief;
    stored.invalidated_question_ids = archive_superseded_brainstorm_turns(stored);
    if let Some(id) = result.consumed_extension_id.as_ref() {
        stored.pending_extensions.retain(|plan| &plan.id != id);
    }
    if let Some(question) = result.question.as_ref() {
        if let Some(parent_id) = question.parent_question_id.as_ref() {
            let parent_revision = stored.user_input_revisions.get(parent_id).copied().unwrap_or(0);
            for (index, goal) in result.sibling_question_goals.into_iter().enumerate() {
                stored.pending_extensions.push(BrainstormExtensionPlan {
                    id: uuid::Uuid::new_v4().to_string(), parent_question_id: parent_id.clone(),
                    parent_option_id: question.parent_option_id.clone(), parent_revision,
                    goal, ordinal: index + 1,
                });
            }
        }
    }
    if result.status == "ready" {
        stored.current_question = None;
        stored.current_extension_id = None;
        stored.continuation_directions = result.continuation_directions;
        "ready".to_string()
    } else {
        if let Some(question) = result.question.as_ref() {
            if let Some(parent_revision) = question.parent_question_id.as_ref()
                .and_then(|parent_id| stored.user_input_revisions.get(parent_id))
            {
                stored.branch_parent_revisions.insert(question.id.clone(), *parent_revision);
            }
        }
        stored.current_question = result.question;
        stored.continuation_directions.clear();
        "exploring".to_string()
    }
}

fn legacy_brainstorm_meta(question_id: &str) -> (&'static str, &'static str) {
    match question_id {
        "outcome.primary" => ("目标与决策", "这次创作最需要推动什么结果？"),
        "audience.primary" => ("目标读者", "这份内容首先写给谁看？"),
        "problem.focus" => ("核心问题", "最应该优先解决哪个问题？"),
        "scope.first_release" => ("首期范围", "第一阶段应该做到什么范围？"),
        "constraints.critical" => ("关键约束", "有哪些不能忽略的硬约束？"),
        "direction.approach" => ("方案方向", "整体方向更适合怎样推进？"),
        "success.criteria" => ("成功标准", "用哪些结果判断方案成功？"),
        _ => ("旧版决定", "旧版脑暴中已确认的决定"),
    }
}

fn legacy_choice_label(option_id: &str) -> &str {
    match option_id {
        "approve" => "推动批准或立项",
        "align" => "统一认知与方案方向",
        "execute" => "指导团队直接执行",
        "management" => "管理层或决策人",
        "business" => "业务负责人",
        "technical" => "产品与技术团队",
        "mixed" => "跨职能评审团队",
        "closed_loop" => "选择关键场景做完整闭环",
        "foundation" => "先建设通用底座与标准",
        "broad" => "首期覆盖主要业务范围",
        "evolve" => "复用现有能力并渐进增强",
        "rebuild" => "建设新的完整方案",
        "compare" => "保留候选方向继续比较",
        other => other,
    }
}

fn brainstorm_question_fingerprint(prompt: &str) -> String {
    prompt
        .chars()
        .filter(|character| character.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

// Independent requests cannot see each other's eventual wording. Recheck
// published siblings at cache consumption, with conservative text similarity.
fn brainstorm_questions_overlap(left: &BrainstormQuestion, right: &BrainstormQuestion) -> bool {
    let left_exact = brainstorm_question_fingerprint(&left.prompt);
    let right_exact = brainstorm_question_fingerprint(&right.prompt);
    if left_exact == right_exact { return true; }
    if left.parent_question_id.is_none()
        || left.parent_question_id != right.parent_question_id
        || left.parent_option_id != right.parent_option_id
    { return false; }
    let normalize = |mut text: String| {
        for scaffolding in ["如何", "怎样", "怎么", "请问", "设计", "愿意", "能够", "可以", "应该", "才能"] {
            text = text.replace(scaffolding, "");
        }
        text
    };
    let left = normalize(left_exact);
    let right = normalize(right_exact);
    let a = left.chars().collect::<Vec<_>>();
    let b = right.chars().collect::<Vec<_>>();
    let shortest = a.len().min(b.len());
    let longest = a.len().max(b.len());
    if shortest >= 6 && shortest * 100 >= longest * 55 && (left.contains(&right) || right.contains(&left)) {
        return true;
    }
    if shortest < 18 || longest > 120 { return false; }
    let mut previous = vec![0usize; b.len() + 1];
    for character in &a {
        let mut row = vec![0usize; b.len() + 1];
        for (index, other) in b.iter().enumerate() {
            row[index + 1] = if character == other { previous[index] + 1 }
                else { row[index].max(previous[index + 1]) };
        }
        previous = row;
    }
    previous[b.len()] * 200 >= (a.len() + b.len()) * 90
}

fn brainstorm_question_repeats_answered(
    turn: &BrainstormStoredTurn,
    candidate: &BrainstormQuestion,
) -> bool {
    if brainstorm_questions_overlap(&turn.question, candidate) {
        return true;
    }
    if turn.answer.source != "user"
        || turn.question.parent_question_id.is_none()
        || turn.question.parent_question_id != candidate.parent_question_id
        || turn.question.parent_option_id != candidate.parent_option_id
    {
        return false;
    }
    turn.answer
        .selected_option_ids
        .iter()
        .filter_map(|id| turn.question.options.iter().find(|option| &option.id == id))
        .any(|selected| {
            let selected_label = brainstorm_question_fingerprint(&selected.label);
            let selected_description = brainstorm_question_fingerprint(&selected.description);
            selected_label.chars().count() >= 4
                && selected_description.chars().count() >= 8
                && candidate.options.iter().any(|option| {
                    let label = brainstorm_question_fingerprint(&option.label);
                    let description = brainstorm_question_fingerprint(&option.description);
                    let shorter = selected_description
                        .chars()
                        .count()
                        .min(description.chars().count());
                    let longer = selected_description
                        .chars()
                        .count()
                        .max(description.chars().count());
                    label == selected_label
                        && description.chars().count() >= 8
                        && shorter * 100 >= longer * 90
                        && (selected_description.contains(&description)
                            || description.contains(&selected_description))
                })
        })
}

fn discard_duplicate_brainstorm_questions(stored: &mut BrainstormStoredState) {
    let mut seen = std::collections::HashSet::new();
    let mut invalidated = Vec::new();
    stored.turns.retain(|turn| {
        let fingerprint = brainstorm_question_fingerprint(&turn.question.prompt);
        if fingerprint.is_empty() || seen.insert(fingerprint) {
            true
        } else {
            invalidated.push(turn.question.id.clone());
            false
        }
    });
    let duplicate_current = stored.current_question.as_ref().is_some_and(|question| {
        let fingerprint = brainstorm_question_fingerprint(&question.prompt);
        (!fingerprint.is_empty() && seen.contains(&fingerprint))
            || stored
                .turns
                .iter()
                .any(|turn| {
                    if stored.current_extension_id.is_some() {
                        brainstorm_questions_overlap(&turn.question, question)
                    } else {
                        brainstorm_question_repeats_answered(turn, question)
                    }
                })
    });
    if duplicate_current {
        if let Some(question) = stored.current_question.take() {
            invalidated.push(question.id);
        }
        stored.current_extension_id = None;
        // These flags came from the same rejected model turn.  A manual edit is
        // independent user input and remains authoritative.
        if !stored.brief_edits.contains_key("open_flags") {
            stored.open_flags.clear();
        }
    }
    for question_id in invalidated {
        if !stored.invalidated_question_ids.contains(&question_id) {
            stored.invalidated_question_ids.push(question_id);
        }
    }
}

fn parse_brainstorm_state(state_json: &str) -> Result<BrainstormStoredState, serde_json::Error> {
    let value = serde_json::from_str::<serde_json::Value>(state_json)?;
    let mut stored = serde_json::from_value::<BrainstormStoredState>(value.clone())?;
    // 新协议不再允许纯自由文本当前题。旧会话恢复时丢弃不合格的当前题，
    // start 分支会用完整决策上下文重新向模型请求带枚举方向的问题。
    if stored
        .current_question
        .as_ref()
        .is_some_and(|question| !is_enumerated_brainstorm_question(question))
    {
        stored.current_question = None;
        stored.current_extension_id = None;
    }
    // 已回答的旧题只作为历史证据展示，不重新进入模型问题协议。
    for turn in &mut stored.turns {
        if turn.question.question_type == "free_text" {
            turn.question.question_type = "confirm_inference".to_string();
        }
    }
    if !stored.turns.is_empty() {
        discard_duplicate_brainstorm_questions(&mut stored);
        return Ok(stored);
    }
    let Some(legacy_answers) = value.get("answers").and_then(|answers| answers.as_object()) else {
        return Ok(stored);
    };
    for (question_id, answer_value) in legacy_answers {
        let mut answer = serde_json::from_value::<BrainstormAnswer>(answer_value.clone())?;
        let mut summary = answer
            .selected_option_ids
            .iter()
            .map(|id| legacy_choice_label(id).to_string())
            .collect::<Vec<_>>();
        if !answer.custom_text.trim().is_empty() {
            summary.push(answer.custom_text.trim().to_string());
        }
        answer.selected_option_ids.clear();
        answer.custom_text = summary.join("；");
        let (dimension, prompt) = legacy_brainstorm_meta(question_id);
        stored.turns.push(BrainstormStoredTurn {
            question: BrainstormQuestion {
                exploration_stage: None,
                parent_question_id: None,
                parent_option_id: None,
                single_choice_reason: String::new(),
                id: question_id.clone(),
                dimension_id: String::new(),
                dimension: dimension.to_string(),
                question_type: "confirm_inference".to_string(),
                prompt: prompt.to_string(),
                why_now: "从旧版脑暴记录迁移的已确认决定。".to_string(),
                context_details: String::new(),
                required: false,
                allow_custom: true,
                options: Vec::new(),
                answer_template: String::new(),
            },
            answer,
        });
    }
    discard_duplicate_brainstorm_questions(&mut stored);
    Ok(stored)
}

fn brainstorm_error(
    status: StatusCode,
    code: &str,
    message: &str,
) -> (StatusCode, Json<serde_json::Value>) {
    (
        status,
        Json(serde_json::json!({ "code": code, "message": message })),
    )
}

fn brainstorm_session_terminated_error() -> (StatusCode, Json<serde_json::Value>) {
    (
        StatusCode::CONFLICT,
        Json(serde_json::json!({
            "code": "BRAINSTORM_SESSION_TERMINATED",
            "message": "当前会话已终止，请开启新会话",
            "retryable": false,
        })),
    )
}

fn brainstorm_history_is_terminated(
    conn: &rusqlite::Connection,
    session_id: &str,
) -> rusqlite::Result<bool> {
    let mut statement = conn.prepare(
        "SELECT conversation_json FROM creation_history
         WHERE session_id = ?1 AND conversation_json IS NOT NULL",
    )?;
    let histories = statement.query_map([session_id], |row| row.get::<_, String>(0))?;
    for history in histories {
        let messages =
            serde_json::from_str::<Vec<serde_json::Value>>(&history?).unwrap_or_default();
        // A stopped run remains retryable. Only the explicit session boundary
        // ends the conversation, regardless of the history lifecycle status.
        if messages
            .iter()
            .any(|message| message["kind"] == "session_end")
        {
            return Ok(true);
        }
    }
    Ok(false)
}

fn with_brainstorm_session_guard<T>(
    state: &AppState,
    session_id: &str,
    allow_terminated: bool,
    operation: impl FnOnce(&rusqlite::Connection) -> Result<T, crate::storage::StorageError>,
) -> Result<T, (StatusCode, Json<serde_json::Value>)> {
    state
        .storage
        .with_conn(|conn| {
            // Check and write under the same connection lock: a late model result
            // cannot pass a check before termination and persist after it.
            if !allow_terminated
                && (brainstorm_history_is_terminated(conn, session_id)?
                    || crate::storage::repo::creation_brainstorm::get(conn, session_id)?
                        .is_some_and(|session| session.phase == "abandoned"))
            {
                return Ok(None);
            }
            operation(conn).map(Some)
        })
        .map_err(|error| {
            error!("检查或保存脑暴会话失败: {}", error);
            brainstorm_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "BRAINSTORM_PERSIST_FAILED",
                "脑暴进度读写失败，请重试",
            )
        })?
        .ok_or_else(brainstorm_session_terminated_error)
}

fn brainstorm_model_error(code: &str) -> (StatusCode, Json<serde_json::Value>) {
    let (status, code, message, retryable) = match code {
        "BRAINSTORM_MODEL_OUTPUT_TRUNCATED" | "CREATION_DOCUMENT_TRUNCATED" => (
            StatusCode::BAD_GATEWAY,
            "BRAINSTORM_MODEL_OUTPUT_TRUNCATED",
            "脑暴问题生成达到长度上限，已保留当前输入，请缩小本轮讨论范围后重试",
            false,
        ),
        "MODEL_ACCESS_DENIED" => (
            StatusCode::FORBIDDEN,
            "MODEL_ACCESS_DENIED",
            "当前模型访问未获授权，请切换可用模型或检查模型权限后重试",
            false,
        ),
        "MODEL_RATE_LIMITED" => (
            StatusCode::TOO_MANY_REQUESTS,
            "MODEL_RATE_LIMITED",
            "模型服务当前繁忙，已保留当前输入，请稍后重试",
            true,
        ),
        "MODEL_REQUEST_FAILED" => (
            StatusCode::BAD_GATEWAY,
            "MODEL_REQUEST_FAILED",
            "模型未能接受脑暴请求，请检查模型配置后重试",
            false,
        ),
        "MODEL_TRANSPORT_UNAVAILABLE" | "MODEL_SERVICE_UNAVAILABLE" | "CREATION_UNAVAILABLE"
        | "BRAINSTORM_MODEL_UNAVAILABLE" => (
            StatusCode::SERVICE_UNAVAILABLE,
            "BRAINSTORM_MODEL_UNAVAILABLE",
            "脑暴问题生成服务暂时不可用，已保留当前输入，请稍后重试",
            true,
        ),
        "BRAINSTORM_MODEL_TIMEOUT" => (
            StatusCode::GATEWAY_TIMEOUT,
            "BRAINSTORM_MODEL_TIMEOUT",
            "脑暴问题生成超时，已保留当前输入，请稍后重试",
            true,
        ),
        _ => (
            StatusCode::BAD_GATEWAY,
            "BRAINSTORM_MODEL_OUTPUT_INVALID",
            "脑暴问题未生成有效结果，已保留当前输入，请重试",
            false,
        ),
    };
    (
        status,
        Json(serde_json::json!({ "code": code, "message": message, "retryable": retryable })),
    )
}

fn brainstorm_sidecar_error(
    status: StatusCode,
    body: &serde_json::Value,
) -> (StatusCode, Json<serde_json::Value>) {
    let code = body.get("detail").unwrap_or(body)
        .get("code").and_then(serde_json::Value::as_str).unwrap_or("");
    let failure = brainstorm_model_error(code);
    if code == "BRAINSTORM_MODEL_OUTPUT_INVALID"
        || failure.1.0["code"] != "BRAINSTORM_MODEL_OUTPUT_INVALID"
    {
        return failure;
    }
    // Unknown codes and arbitrary upstream messages are never forwarded.
    match status {
        StatusCode::TOO_MANY_REQUESTS => brainstorm_model_error("MODEL_RATE_LIMITED"),
        StatusCode::SERVICE_UNAVAILABLE => brainstorm_model_error("BRAINSTORM_MODEL_UNAVAILABLE"),
        StatusCode::GATEWAY_TIMEOUT => brainstorm_model_error("BRAINSTORM_MODEL_TIMEOUT"),
        _ => failure,
    }
}

/// POST /api/creation/brainstorm/turn - 逐答持久化的创作前置脑暴状态机。
pub async fn run_creation_brainstorm_turn(
    State(state): State<Arc<AppState>>,
    headers: HeaderMap,
    Json(req): Json<BrainstormTurnRequest>,
) -> Response {
    let wants_events = headers.get_all(header::ACCEPT).iter().any(|value| {
        value.to_str().is_ok_and(|value| {
            value.split(',').any(|media_type| {
                let mut parts = media_type.split(';');
                parts.next().unwrap_or("").trim()
                    .eq_ignore_ascii_case("text/event-stream")
                    && parts.all(|parameter| {
                        let Some((name, value)) = parameter.trim().split_once('=') else {
                            return true;
                        };
                        !name.trim().eq_ignore_ascii_case("q")
                            || value.trim().parse::<f32>().is_ok_and(|q| q > 0.0 && q <= 1.0)
                    })
            })
        })
    });
    if !wants_events {
        return execute_creation_brainstorm_turn(state, req).await.into_response();
    }

    // Send headers before entering the inference queue. A JSON-only response can
    // stay silent longer than WebKit's request timeout even within our 180s budget.
    // Own the future in the body: dropping the connection still cancels this turn.
    let stream = async_stream::stream! {
        yield Ok::<Event, Infallible>(Event::default()
            .event("brainstorm.started").data("{}"));
        let (name, data) = match execute_creation_brainstorm_turn(state, req).await {
            Ok(Json(result)) => ("brainstorm.completed", serde_json::json!({ "state": result })),
            Err((status, Json(mut failure))) => {
                failure["status"] = serde_json::json!(status.as_u16());
                ("brainstorm.failed", failure)
            }
        };
        // The existing state machine validates and persists before returning.
        yield Ok(Event::default().event(name).data(data.to_string()));
    };
    Sse::new(stream).keep_alive(
        axum::response::sse::KeepAlive::new()
            .interval(Duration::from_secs(10))
            .text("keep-alive"),
    ).into_response()
}

async fn execute_creation_brainstorm_turn(
    state: Arc<AppState>,
    req: BrainstormTurnRequest,
) -> Result<Json<BrainstormTurnResponse>, (StatusCode, Json<serde_json::Value>)> {
    let mut trace = BrainstormTurnTrace {
        session_id: brainstorm_log_identifier(&req.session_id).to_string(),
        action: brainstorm_log_action(&req.action).to_string(),
        started: Instant::now(),
        finished: false,
    };
    log_brainstorm_stage(&req, "started", trace.started);
    let result = run_creation_brainstorm_turn_inner(state, &req, trace.started).await;
    log_brainstorm_stage(
        &req,
        if result.is_ok() { "completed" } else { "failed" },
        trace.started,
    );
    trace.finished = true;
    result
}

async fn run_creation_brainstorm_turn_inner(
    state: Arc<AppState>,
    req: &BrainstormTurnRequest,
    started: Instant,
) -> Result<Json<BrainstormTurnResponse>, (StatusCode, Json<serde_json::Value>)> {
    let session_id = req.session_id.trim();
    let root_request = req.root_request.trim();
    if session_id.is_empty() || root_request.is_empty() {
        return Err(brainstorm_error(
            StatusCode::BAD_REQUEST,
            "BRAINSTORM_INVALID_REQUEST",
            "session_id 和 root_request 不能为空",
        ));
    }

    use crate::storage::repo::creation_brainstorm;
    let (existing, history_terminated) = state
        .storage
        .with_conn(|conn| {
            Ok((
                creation_brainstorm::get(conn, session_id)?,
                brainstorm_history_is_terminated(conn, session_id)?,
            ))
        })
        .map_err(|error| {
            error!("读取脑暴会话失败: {}", error);
            brainstorm_error(
                StatusCode::INTERNAL_SERVER_ERROR,
                "BRAINSTORM_PERSIST_FAILED",
                "脑暴进度读取失败，请重试",
            )
        })?;

    if history_terminated && req.action != "abandon" {
        return Err(brainstorm_session_terminated_error());
    }

    if req.action == "start" {
        if let Some(session) = existing {
            let mut stored = parse_brainstorm_state(&session.state_json).map_err(|error| {
                error!("脑暴会话解析失败: {}", error);
                brainstorm_error(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "BRAINSTORM_STATE_INVALID",
                    "已有脑暴进度无法恢复，请重新开始",
                )
            })?;
            if session.phase == "exploring" && stored.current_question.is_none() {
                initialize_brainstorm_input_revisions(&mut stored, session.revision);
                let step =
                    generate_dynamic_brainstorm_step(&state, &stored, &req, false, false, "")
                        .await?;
                let phase = apply_dynamic_step(&mut stored, step);
                let changed = with_brainstorm_session_guard(&state, session_id, false, |conn| {
                    Ok(creation_brainstorm::update(
                        conn,
                        session_id,
                        session.revision,
                        &phase,
                        &serde_json::to_string(&stored)?,
                    )?)
                })?;
                if !changed {
                    return Err(brainstorm_error(
                        StatusCode::CONFLICT,
                        "BRAINSTORM_REVISION_CONFLICT",
                        "脑暴内容已更新，请刷新后重试",
                    ));
                }
                log_brainstorm_stage(req, "persisted", started);
                brainstorm_prefetch::schedule(&state, &stored, req, &phase, session.revision + 1);
                return Ok(Json(brainstorm_response(
                    session_id,
                    &phase,
                    session.revision + 1,
                    &stored,
                )));
            }
            brainstorm_prefetch::schedule(&state, &stored, req, &session.phase, session.revision);
            return Ok(Json(brainstorm_response(
                session_id,
                &session.phase,
                session.revision,
                &stored,
            )));
        }
        let mut stored = BrainstormStoredState {
            pending_extensions: Vec::new(),
            current_extension_id: None,
            prefetch_safe_options: Default::default(),
            branch_parent_revisions: Default::default(),
            memory_brief: String::new(),
            archived_questions: Vec::new(),
            brief_edits: Default::default(),
            user_input_revisions: [("root_request".to_string(), 0)].into_iter().collect(),
            root_request: root_request.to_string(),
            selected_skills: req.selected_skills.clone(),
            turns: Vec::new(),
            current_question: None,
            open_flags: Vec::new(),
            readiness_reason: String::new(),
            continuation_directions: Vec::new(),
            invalidated_question_ids: Vec::new(),
        };
        let step =
            generate_dynamic_brainstorm_step(&state, &stored, &req, false, false, "").await?;
        let phase = apply_dynamic_step(&mut stored, step);
        let session = with_brainstorm_session_guard(&state, session_id, false, |conn| {
            Ok(creation_brainstorm::create(
                conn,
                session_id,
                root_request,
                &phase,
                &serde_json::to_string(&stored)?,
            )?)
        })?;
        log_brainstorm_stage(req, "persisted", started);
        brainstorm_prefetch::schedule(&state, &stored, req, &phase, session.revision);
        return Ok(Json(brainstorm_response(
            session_id,
            &phase,
            session.revision,
            &stored,
        )));
    }

    let Some(session) = existing else {
        return Err(brainstorm_error(
            StatusCode::NOT_FOUND,
            "BRAINSTORM_SESSION_NOT_FOUND",
            "脑暴会话不存在",
        ));
    };
    if session.phase == "abandoned" && req.action != "abandon" {
        return Err(brainstorm_session_terminated_error());
    }
    if session.phase != "abandoned" && req.revision.unwrap_or(-1) != session.revision {
        return Err(brainstorm_error(
            StatusCode::CONFLICT,
            "BRAINSTORM_REVISION_CONFLICT",
            "脑暴内容已更新，请刷新后重试",
        ));
    }
    let mut stored = parse_brainstorm_state(&session.state_json).map_err(|error| {
        error!("脑暴会话解析失败: {}", error);
        brainstorm_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            "BRAINSTORM_STATE_INVALID",
            "已有脑暴进度无法恢复，请重新开始",
        )
    })?;
    if session.phase == "abandoned" {
        return Ok(Json(brainstorm_response(
            session_id,
            &session.phase,
            session.revision,
            &stored,
        )));
    }
    // 兼容在章节覆盖能力上线前创建的会话：客户端每轮都会携带当前执行
    // Skill，旧状态只补写一次，之后仍以服务端持久化上下文为准。
    if stored.selected_skills.is_empty() && !req.selected_skills.is_empty() {
        stored.selected_skills = req.selected_skills.clone();
    }
    initialize_brainstorm_input_revisions(&mut stored, session.revision);

    if !brainstorm_prefetch::answer_preserves_candidates(&stored, req) {
        brainstorm_prefetch::invalidate_revision(&state, session_id, session.revision);
    }
    if !matches!(req.action.as_str(), "answer" | "skip" | "exclude")
        || (req.action == "answer" && !brainstorm_prefetch::answer_preserves_candidates(&stored, req))
    {
        stored.pending_extensions.clear();
    }
    let next_phase = match req.action.as_str() {
        "edit_brief" => {
            if req.brief_edits.iter().any(|(key, value)| {
                value.chars().count() > 12000
                    || !(key == "root_request"
                        || key == "open_flags"
                        || stored.turns.iter().any(|turn| &turn.question.id == key))
            }) {
                return Err(brainstorm_error(
                    StatusCode::BAD_REQUEST,
                    "BRAINSTORM_INVALID_BRIEF",
                    "简报字段无效或超过 12000 字",
                ));
            }
            // A manual edit can revoke source permission. Derived references
            // must not survive while a replacement question is being prepared.
            stored.memory_brief.clear();
            stored.brief_edits.extend(req.brief_edits.clone());
            for key in req.brief_edits.keys() {
                stored.user_input_revisions.insert(key.clone(), session.revision + 1);
            }
            session.phase.clone()
        }
        "answer" | "skip" | "exclude" => {
            let Some(question) = stored.current_question.clone() else {
                return Err(brainstorm_error(
                    StatusCode::CONFLICT,
                    "BRAINSTORM_QUESTION_STALE",
                    "当前问题已变化，请按最新问题回答",
                ));
            };
            if req.question_id.as_deref() != Some(question.id.as_str()) {
                return Err(brainstorm_error(
                    StatusCode::CONFLICT,
                    "BRAINSTORM_QUESTION_STALE",
                    "当前问题已变化，请按最新问题回答",
                ));
            }
            let answer = if req.action == "exclude" {
                BrainstormAnswer {
                    selected_option_ids: Vec::new(),
                    custom_text: "该方向不重要".to_string(),
                    source: "user_excluded".to_string(),
                }
            } else if req.action == "skip" {
                BrainstormAnswer {
                    selected_option_ids: Vec::new(),
                    custom_text: "暂未确定，生成时由创作 Agent 补充并保留为待核验假设".to_string(),
                    source: "agent_assumption".to_string(),
                }
            } else {
                let answer = req.answer.clone().unwrap_or_default();
                if !validate_brainstorm_answer(&question, &answer) {
                    return Err(brainstorm_error(
                        StatusCode::BAD_REQUEST,
                        "BRAINSTORM_INVALID_ANSWER",
                        "答案与当前题型不匹配",
                    ));
                }
                BrainstormAnswer {
                    source: "user".to_string(),
                    ..answer
                }
            };
            stored.user_input_revisions.insert(question.id.clone(), session.revision + 1);
            stored.turns.push(BrainstormStoredTurn { question, answer });
            stored.current_question = None;
            stored.current_extension_id = None;
            let step =
                generate_dynamic_brainstorm_step(&state, &stored, &req, false, false, "").await?;
            apply_dynamic_step(&mut stored, step)
        }
        "reopen" => {
            let Some(question_id) = req.question_id.as_deref() else {
                return Err(brainstorm_error(
                    StatusCode::CONFLICT,
                    "BRAINSTORM_QUESTION_STALE",
                    "要修改的决定不存在",
                ));
            };
            let Some(index) = stored
                .turns
                .iter()
                .position(|turn| turn.question.id == question_id)
            else {
                return Err(brainstorm_error(
                    StatusCode::CONFLICT,
                    "BRAINSTORM_QUESTION_STALE",
                    "要修改的决定不存在",
                ));
            };
            let previous_current = stored.current_question.take();
            let removed = stored.turns.split_off(index);
            for turn in &removed {
                stored.brief_edits.remove(&turn.question.id);
                stored.user_input_revisions.remove(&turn.question.id);
                stored.branch_parent_revisions.remove(&turn.question.id);
            }
            stored.current_question = removed.first().map(|turn| turn.question.clone());
            stored.current_extension_id = None;
            stored
                .archived_questions
                .extend(
                    removed
                        .iter()
                        .skip(1)
                        .map(|turn| BrainstormArchivedQuestion {
                            question: turn.question.clone(),
                            answer: Some(turn.answer.clone()),
                        }),
                );
            let mut invalidated_question_ids = removed
                .iter()
                .map(|turn| turn.question.id.clone())
                .collect::<Vec<_>>();
            if let Some(question) = previous_current {
                stored.archived_questions.push(BrainstormArchivedQuestion {
                    question: question.clone(),
                    answer: None,
                });
                invalidated_question_ids.push(question.id);
            }
            stored.invalidated_question_ids = invalidated_question_ids;
            stored.open_flags = stored
                .current_question
                .iter()
                .map(|question| question.prompt.clone())
                .collect();
            stored.readiness_reason = "上游决定已重新打开，后续分支将按新答案重新生成".to_string();
            stored.continuation_directions.clear();
            "exploring".to_string()
        }
        "revise_answer" => {
            let Some(question_id) = req.question_id.as_deref() else {
                return Err(brainstorm_error(
                    StatusCode::BAD_REQUEST,
                    "BRAINSTORM_INVALID_ANSWER",
                    "缺少要修改的问题",
                ));
            };
            let Some(index) = stored
                .turns
                .iter()
                .position(|turn| turn.question.id == question_id)
            else {
                return Err(brainstorm_error(
                    StatusCode::CONFLICT,
                    "BRAINSTORM_QUESTION_STALE",
                    "要修改的决定不存在",
                ));
            };
            let question = stored.turns[index].question.clone();
            let answer = req.answer.clone().unwrap_or_default();
            if !validate_brainstorm_answer(&question, &answer) {
                return Err(brainstorm_error(
                    StatusCode::BAD_REQUEST,
                    "BRAINSTORM_INVALID_ANSWER",
                    "答案与当前题型不匹配",
                ));
            }
            let mut invalidated = stored.turns[index + 1..]
                .iter()
                .map(|turn| turn.question.id.clone())
                .collect::<Vec<_>>();
            for turn in &stored.turns[index..] {
                stored.brief_edits.remove(&turn.question.id);
                stored.user_input_revisions.remove(&turn.question.id);
                stored.branch_parent_revisions.remove(&turn.question.id);
            }
            stored
                .archived_questions
                .extend(
                    stored.turns[index + 1..]
                        .iter()
                        .map(|turn| BrainstormArchivedQuestion {
                            question: turn.question.clone(),
                            answer: Some(turn.answer.clone()),
                        }),
                );
            if let Some(question) = stored.current_question.take() {
                invalidated.push(question.id.clone());
                stored.archived_questions.push(BrainstormArchivedQuestion {
                    question,
                    answer: None,
                });
            }
            stored.turns.truncate(index);
            stored.user_input_revisions.insert(question.id.clone(), session.revision + 1);
            stored.turns.push(BrainstormStoredTurn {
                question,
                answer: BrainstormAnswer {
                    source: "user".to_string(),
                    ..answer
                },
            });
            stored.current_question = None;
            stored.current_extension_id = None;
            let step =
                generate_dynamic_brainstorm_step(&state, &stored, &req, false, false, "").await?;
            let phase = apply_dynamic_step(&mut stored, step);
            stored.invalidated_question_ids = invalidated;
            phase
        }
        "finish" => {
            let current_superseded = stored.current_question.as_ref()
                .is_some_and(|question| !brainstorm_question_is_current(&stored, question));
            stored.invalidated_question_ids = archive_superseded_brainstorm_turns(&mut stored);
            if current_superseded {
                if let Some(question) = stored.current_question.take() {
                    stored.invalidated_question_ids.push(question.id.clone());
                    stored.archived_questions.push(BrainstormArchivedQuestion {question, answer: None});
                }
                stored.current_extension_id = None;
            }
            if stored
                .current_question
                .as_ref()
                .is_some_and(|question| question.required)
                && !req.accept_assumptions
            {
                return Err(brainstorm_error(
                    StatusCode::UNPROCESSABLE_ENTITY,
                    "BRAINSTORM_NOT_READY",
                    "仍有关键方向未确认",
                ));
            }
            if let Some(question) = stored.current_question.take() {
                if !stored
                    .open_flags
                    .iter()
                    .any(|item| item == &question.prompt)
                {
                    stored.open_flags.insert(0, question.prompt);
                }
            }
            stored.current_extension_id = None;
            stored.readiness_reason = "用户选择基于当前简报开始生成".to_string();
            stored.continuation_directions.clear();
            "ready".to_string()
        }
        "change_direction" => {
            let step =
                generate_dynamic_brainstorm_step(&state, &stored, &req, false, true, "").await?;
            stored.open_flags = step.open_flags;
            stored.readiness_reason = step.readiness_reason;
            stored.memory_brief = step.memory_brief;
            stored.continuation_directions = step.continuation_directions;
            stored.invalidated_question_ids.clear();
            "choosing_direction".to_string()
        }
        "continue_brainstorm" => {
            if !matches!(session.phase.as_str(), "ready" | "choosing_direction") {
                return Err(brainstorm_error(
                    StatusCode::CONFLICT,
                    "BRAINSTORM_NOT_READY",
                    "当前脑暴尚未收敛，无需手动选择继续方向",
                ));
            }
            if let Some(question) = stored.current_question.take() {
                stored.archived_questions.push(BrainstormArchivedQuestion {
                    question,
                    answer: None,
                });
            }
            stored.current_extension_id = None;
            let custom_focus = if req.continuation_direction_id == "__custom__" {
                let custom = req.focus_hint.trim();
                if custom.is_empty() || custom.chars().count() > 500 {
                    return Err(brainstorm_error(StatusCode::BAD_REQUEST,
                        "BRAINSTORM_INVALID_CONTINUATION_DIRECTION", "请输入 1 到 500 字的脑暴方向"));
                }
                custom.to_string()
            } else {
                String::new()
            };
            if !req.continuation_direction_ids.is_empty() {
                let ids = &req.continuation_direction_ids;
                let unique: std::collections::HashSet<_> = ids.iter().collect();
                if unique.len() != ids.len()
                    || ids.iter().any(|id| {
                        !stored
                            .continuation_directions
                            .iter()
                            .any(|direction| &direction.id == id)
                    })
                {
                    return Err(brainstorm_error(
                        StatusCode::BAD_REQUEST,
                        "BRAINSTORM_INVALID_CONTINUATION_DIRECTION",
                        "请选择有效的脑暴方向",
                    ));
                }
                let mut question = BrainstormQuestion {
                    exploration_stage: Some("explore".to_string()),
                    id: format!("continuation_{}", session.revision),
                    parent_question_id: None,
                    parent_option_id: None,
                    single_choice_reason: String::new(),
                    dimension_id: "continuation_directions".to_string(),
                    dimension: "继续脑暴方向".to_string(),
                    question_type: "multi_choice".to_string(),
                    prompt: format!("第 {} 轮继续探索哪些方向？", session.revision + 1),
                    why_now: "选中的方向将逐个展开讨论。".to_string(),
                    context_details: String::new(),
                    required: false,
                    allow_custom: true,
                    answer_template: String::new(),
                    options: stored
                        .continuation_directions
                        .iter()
                        .map(BrainstormOption::from)
                        .collect(),
                };
                let mut selected_option_ids = ids.clone();
                if !custom_focus.is_empty() {
                    let mut custom_id = "__user_custom__".to_string();
                    while question.options.iter().any(|option| option.id == custom_id) {
                        custom_id.push('_');
                    }
                    question.options.push(BrainstormOption {id: custom_id.clone(),
                        label: custom_focus.clone(), description: "用户自定义方向".to_string(),
                        details: String::new(), recommended: false});
                    selected_option_ids.push(custom_id);
                }
                stored.user_input_revisions.insert(question.id.clone(), session.revision + 1);
                stored.turns.push(BrainstormStoredTurn {
                    question,
                    answer: BrainstormAnswer {
                        selected_option_ids,
                        custom_text: String::new(),
                        source: "user".to_string(),
                    },
                });
                let step = generate_dynamic_brainstorm_step(&state, &stored, &req, true, false, "")
                    .await?;
                apply_dynamic_step(&mut stored, step)
            } else {
                let direction_id = req.continuation_direction_id.trim();
                let focus_hint = if direction_id == "__custom__" {
                    custom_focus
                } else {
                    let Some(direction) = stored
                        .continuation_directions
                        .iter()
                        .find(|direction| direction.id == direction_id)
                    else {
                        return Err(brainstorm_error(
                            StatusCode::BAD_REQUEST,
                            "BRAINSTORM_INVALID_CONTINUATION_DIRECTION",
                            "请选择模型推荐的脑暴方向，或输入自定义方向",
                        ));
                    };
                    // The selected label is persisted below and scheduled by
                    // the same branch queue as a multi-selection.
                    let _ = direction;
                    String::new()
                };
                let question = BrainstormQuestion {
                    exploration_stage: Some("explore".to_string()),
                    id: format!("continuation_{}", session.revision),
                    parent_question_id: None,
                    parent_option_id: None,
                    single_choice_reason: String::new(),
                    dimension_id: "continuation_directions".to_string(),
                    dimension: "继续脑暴方向".to_string(),
                    question_type: "multi_choice".to_string(),
                    prompt: "接下来希望探索哪个方向？".to_string(),
                    why_now: "用户主动继续探索。".to_string(),
                    context_details: String::new(),
                    required: false,
                    allow_custom: true,
                    answer_template: String::new(),
                    options: stored.continuation_directions.iter().map(BrainstormOption::from).collect(),
                };
                stored.user_input_revisions.insert(question.id.clone(), session.revision + 1);
                stored.turns.push(BrainstormStoredTurn {
                    question,
                    answer: BrainstormAnswer {
                        selected_option_ids: if direction_id == "__custom__" { Vec::new() } else { vec![direction_id.to_string()] },
                        custom_text: focus_hint.clone(), source: "user".to_string(),
                    },
                });
                let step = generate_dynamic_brainstorm_step(
                    &state,
                    &stored,
                    &req,
                    true,
                    false,
                    &focus_hint,
                )
                .await?;
                apply_dynamic_step(&mut stored, step)
            }
        }
        "abandon" => "abandoned".to_string(),
        _ => {
            return Err(brainstorm_error(
                StatusCode::BAD_REQUEST,
                "BRAINSTORM_INVALID_ACTION",
                "不支持的脑暴动作",
            ))
        }
    };

    let changed = with_brainstorm_session_guard(
        &state,
        session_id,
        req.action == "abandon",
        |conn| {
            Ok(creation_brainstorm::update(
                conn,
                session_id,
                session.revision,
                &next_phase,
                &serde_json::to_string(&stored)?,
            )?)
        },
    )?;
    if !changed {
        return Err(brainstorm_error(
            StatusCode::CONFLICT,
            "BRAINSTORM_REVISION_CONFLICT",
            "脑暴内容已更新，请刷新后重试",
        ));
    }
    log_brainstorm_stage(req, "persisted", started);
    brainstorm_prefetch::schedule(&state, &stored, req, &next_phase, session.revision + 1);
    Ok(Json(brainstorm_response(
        session_id,
        &next_phase,
        session.revision + 1,
        &stored,
    )))
}

pub async fn preview_references(
    State(state): State<Arc<AppState>>,
    Json(req): Json<GenerateRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let payload = ReferencePayload {
        user_prompt: req.user_prompt,
        doc_type: req.doc_type,
        audience: req.audience,
        inherit_format: req.inherit_format,
        enable_rag: req.enable_rag,
        content_weight: req.content_weight,
        quality_weight: req.quality_weight,
        completeness_weight: req.completeness_weight,
        usage_weight: req.usage_weight,
        format_weight: req.format_weight,
        freshness_weight: req.freshness_weight,
        max_references: req.max_references,
    };

    let client = creation_sidecar_client();
    let response = client
        .post(format!("{}/creation/references", state.creation_sidecar_url))
        .json(&payload)
        .send()
        .await
        .map_err(|e| {
            error!("调用 ai-sidecar 参考资料预览失败: {}", e);
            (StatusCode::BAD_GATEWAY, format!("AI 服务不可用: {}", e))
        })?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        error!("ai-sidecar 参考资料预览返回错误: {} - {}", status, body);
        return Err((StatusCode::BAD_GATEWAY, format!("AI 服务错误: {}", body)));
    }

    let body = response.json::<serde_json::Value>().await.map_err(|e| {
        error!("解析参考资料预览响应失败: {}", e);
        (
            StatusCode::BAD_GATEWAY,
            format!("AI 服务响应格式错误: {}", e),
        )
    })?;

    Ok(Json(body))
}

fn default_true() -> bool {
    true
}

fn required_creation_tool_ids() -> Vec<String> {
    vec![
        "internet_search".to_string(),
        "memory_search".to_string(),
        "data_search".to_string(),
        "webpage_scrape".to_string(),
    ]
}

fn default_creation_tool_ids() -> Vec<String> {
    let mut defaults = required_creation_tool_ids();
    defaults.push("mermaid_diagram".to_string());
    defaults
}

fn normalize_creation_tool_ids(tool_ids: Vec<String>) -> Vec<String> {
    let mut normalized = required_creation_tool_ids();
    for tool_id in tool_ids {
        let tool_id = tool_id.trim();
        if tool_id.is_empty() || normalized.iter().any(|item| item == tool_id) {
            continue;
        }
        normalized.push(tool_id.to_string());
    }
    normalized
}

fn default_output_format() -> String {
    "markdown".to_string()
}

fn default_content_weight() -> f64 {
    0.45
}

fn default_quality_weight() -> f64 {
    0.15
}

fn default_completeness_weight() -> f64 {
    0.15
}

fn default_usage_weight() -> f64 {
    0.10
}

fn default_format_weight() -> f64 {
    0.10
}

fn default_freshness_weight() -> f64 {
    0.05
}

fn default_max_references() -> i64 {
    10
}

fn default_data_search_limit() -> usize {
    30
}

fn default_model_mode() -> String {
    "local".to_string()
}

fn default_creation_mode() -> String {
    "direct".to_string()
}

fn default_execution_origin() -> String {
    "interactive".to_string()
}

fn default_brainstorm_answer_source() -> String {
    "user".to_string()
}

fn append_sse_chunk(buffer: &mut Vec<u8>, chunk: &[u8]) -> Vec<String> {
    buffer.extend_from_slice(chunk);
    let mut events = Vec::new();
    while let Some(newline_index) = buffer.iter().position(|byte| *byte == b'\n') {
        let mut line = buffer.drain(..=newline_index).collect::<Vec<_>>();
        line.pop();
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        if let Some(content) = line.strip_prefix(b"data: ") {
            events.push(String::from_utf8_lossy(content).into_owned());
        }
    }
    events
}

fn take_sse_tail(buffer: &mut Vec<u8>) -> Option<String> {
    while buffer
        .last()
        .is_some_and(|byte| matches!(byte, b'\r' | b'\n' | b' ' | b'\t'))
    {
        buffer.pop();
    }
    buffer
        .strip_prefix(b"data: ")
        .map(|content| String::from_utf8_lossy(content).into_owned())
}

fn normalize_conversation_item(value: &serde_json::Value) -> Option<serde_json::Value> {
    let role = value.get("role")?.as_str()?.trim();
    let content = value.get("content")?.as_str()?.trim();
    if !matches!(role, "user" | "assistant") || content.is_empty() {
        return None;
    }
    Some(serde_json::json!({
        "role": role,
        "content": content.chars().take(12_000).collect::<String>(),
    }))
}

fn creation_checkpoint_inputs_match(req: &AgentRunRequest, checkpoint: &serde_json::Value) -> bool {
    if let Some(brief) = &req.creation_brief {
        let saved = &checkpoint["environment"]["creation_brief"];
        let both_empty = brief.as_object().is_some_and(|value| value.is_empty())
            && (saved.is_null() || saved.as_object().is_some_and(|value| value.is_empty()));
        if !both_empty && brief != saved {
            return false;
        }
    }
    if let (Some(current_root), Some(saved_root)) = (
        req.root_request.as_deref(), checkpoint["root_request"].as_str(),
    ) {
        if current_root.trim() != saved_root.trim() {
            return false;
        }
    }
    true
}

fn merge_creation_conversation(
    stored: Vec<serde_json::Value>,
    current: Vec<serde_json::Value>,
    root_request: Option<&str>,
) -> Vec<serde_json::Value> {
    let mut merged = Vec::new();
    let mut stored_counts = HashMap::<String, usize>::new();
    let normalized_root = root_request
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if let Some(root) = normalized_root {
        let item = serde_json::json!({"role": "user", "content": root});
        let key = format!("user\u{1f}{}", root);
        *stored_counts.entry(key).or_default() += 1;
        merged.push(item);
    }
    for value in stored {
        let Some(item) = normalize_conversation_item(&value) else {
            continue;
        };
        let role = item["role"].as_str().unwrap_or_default();
        let content = item["content"].as_str().unwrap_or_default();
        let key = format!("{role}\u{1f}{content}");
        if role == "user"
            && Some(content) == normalized_root
            && stored_counts.get(&key).copied().unwrap_or_default() > 0
        {
            continue;
        }
        *stored_counts.entry(key).or_default() += 1;
        merged.push(item);
    }
    let mut current_counts = HashMap::<String, usize>::new();
    for value in current {
        let Some(item) = normalize_conversation_item(&value) else {
            continue;
        };
        let role = item["role"].as_str().unwrap_or_default();
        let content = item["content"].as_str().unwrap_or_default();
        let key = format!("{role}\u{1f}{content}");
        let occurrence = current_counts.entry(key.clone()).or_default();
        *occurrence += 1;
        if *occurrence <= stored_counts.get(&key).copied().unwrap_or_default() {
            continue;
        }
        merged.push(item);
    }
    if merged.len() <= 60 {
        return merged;
    }
    let tail = merged.split_off(merged.len() - 56);
    merged.truncate(4);
    merged.extend(tail);
    merged
}

fn normalize_history_conversation_item(value: &serde_json::Value) -> Option<serde_json::Value> {
    let normalized = normalize_conversation_item(value)?;
    let mut object = value.as_object().cloned().unwrap_or_default();
    object.insert("role".to_string(), normalized["role"].clone());
    object.insert("content".to_string(), normalized["content"].clone());
    Some(serde_json::Value::Object(object))
}

fn merge_creation_history_conversation(
    stored: Vec<serde_json::Value>,
    current: Vec<serde_json::Value>,
    root_request: Option<&str>,
) -> Vec<serde_json::Value> {
    let mut merged = Vec::new();
    let mut stored_counts = HashMap::<String, usize>::new();
    for value in stored {
        let Some(item) = normalize_history_conversation_item(&value) else {
            continue;
        };
        let role = item["role"].as_str().unwrap_or_default();
        let content = item["content"].as_str().unwrap_or_default();
        *stored_counts
            .entry(format!("{role}\u{1f}{content}"))
            .or_default() += 1;
        merged.push(item);
    }

    let mut current_counts = HashMap::<String, usize>::new();
    for value in current {
        let Some(item) = normalize_history_conversation_item(&value) else {
            continue;
        };
        let role = item["role"].as_str().unwrap_or_default();
        let content = item["content"].as_str().unwrap_or_default();
        let key = format!("{role}\u{1f}{content}");
        let occurrence = current_counts.entry(key.clone()).or_default();
        *occurrence += 1;
        if *occurrence <= stored_counts.get(&key).copied().unwrap_or_default() {
            continue;
        }
        merged.push(item);
    }

    if let Some(root) = root_request
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        let has_root = merged.iter().any(|item| {
            item["role"].as_str() == Some("user") && item["content"].as_str() == Some(root)
        });
        if !has_root {
            merged.insert(0, serde_json::json!({"role": "user", "content": root}));
        }
    }

    if merged.len() <= 60 {
        return merged;
    }
    let tail = merged.split_off(merged.len() - 56);
    merged.truncate(4);
    merged.extend(tail);
    merged
}

fn merge_creation_history_trace(
    stored: Vec<serde_json::Value>,
    current: Vec<serde_json::Value>,
) -> Vec<serde_json::Value> {
    let mut merged = stored;
    let mut positions = HashMap::<String, usize>::new();
    for (index, event) in merged.iter().enumerate() {
        if let Some(event_id) = event
            .get("event_id")
            .and_then(|value| value.as_str())
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            positions.insert(event_id.to_string(), index);
        }
    }

    for event in current {
        let event_id = event
            .get("event_id")
            .and_then(|value| value.as_str())
            .map(str::trim)
            .filter(|value| !value.is_empty());
        if let Some(index) = event_id.and_then(|value| positions.get(value).copied()) {
            merged[index] = event;
            continue;
        }
        if event_id.is_none() && merged.contains(&event) {
            continue;
        }
        if let Some(event_id) = event_id {
            positions.insert(event_id.to_string(), merged.len());
        }
        merged.push(event);
    }

    if merged.len() <= 240 {
        return merged;
    }
    let tail = merged.split_off(merged.len() - 236);
    merged.truncate(4);
    merged.extend(tail);
    merged
}

fn user_instruction_count(conversation: &[serde_json::Value], instruction: &str) -> usize {
    let instruction = instruction.trim();
    conversation
        .iter()
        .filter(|value| {
            normalize_conversation_item(value).is_some_and(|item| {
                item["role"].as_str() == Some("user")
                    && item["content"].as_str() == Some(instruction)
            })
        })
        .count()
}

fn ensure_current_creation_instruction(
    conversation: &mut Vec<serde_json::Value>,
    stored: &[serde_json::Value],
    instruction: &str,
) {
    let instruction = instruction.trim();
    if instruction.is_empty()
        || user_instruction_count(conversation, instruction)
            > user_instruction_count(stored, instruction)
    {
        return;
    }

    let insert_at = conversation
        .iter()
        .rposition(|value| {
            normalize_conversation_item(value)
                .is_some_and(|item| item["role"].as_str() == Some("assistant"))
        })
        .unwrap_or(conversation.len());
    let created_at = chrono::Utc::now().timestamp_millis();
    conversation.insert(
        insert_at,
        serde_json::json!({
            "id": format!("server-user-{created_at}"),
            "role": "user",
            "content": instruction.chars().take(12_000).collect::<String>(),
            "createdAt": created_at,
        }),
    );
}

fn conversation_root_request(conversation: &[serde_json::Value]) -> Option<String> {
    conversation.iter().find_map(|value| {
        let normalized = normalize_conversation_item(value)?;
        (normalized["role"].as_str() == Some("user")).then(|| {
            normalized["content"]
                .as_str()
                .unwrap_or_default()
                .to_string()
        })
    })
}

fn normalize_edit_operation(value: &str) -> &str {
    match value {
        "create_document"
        | "append_section"
        | "replace_section"
        | "delete_section"
        | "rewrite_document"
        | "revise_document"
        | "polish_selection"
        | "expand_selection"
        | "elaborate_selection"
        | "undo_inline_edit" => value,
        _ => "rewrite_document",
    }
}

fn creation_model_name(id: &str) -> &str {
    match id {
        "mbcd-plus-v1" => "claude-opus-4-8",
        "mbcd-std-v1" => "qwen3.5:4b",
        "claude-opus-4-8" => "claude-opus-4-8",
        "qwen-3-5-4b" => "qwen3.5:4b",
        _ => id,
    }
}

fn enrich_creation_model_from_preferences(state: &Arc<AppState>, req: &mut GenerateRequest) {
    if req.creation_model.is_some() {
        return;
    }

    let Some(raw) = state
        .storage
        .get_preference_value("creation.models")
        .ok()
        .flatten()
    else {
        return;
    };
    let Ok(models) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return;
    };
    let Some(items) = models.as_array() else {
        return;
    };
    let Some(selected) = items.iter().find(|item| {
        item.get("enabled")
            .and_then(|value| value.as_bool())
            .unwrap_or(false)
    }) else {
        return;
    };
    let Some(id) = selected.get("id").and_then(|value| value.as_str()) else {
        return;
    };
    let api_key = selected
        .get("apiKey")
        .and_then(|value| value.as_str())
        .filter(|value| !value.trim().is_empty())
        .map(|value| value.to_string());

    if id != "mbcd-std-v1" && api_key.is_none() {
        return;
    }

    req.creation_model = Some(creation_model_name(id).to_string());
    req.creation_api_key = api_key;
    req.creation_base_url = selected
        .get("baseUrl")
        .and_then(|value| value.as_str())
        .filter(|value| !value.trim().is_empty())
        .map(|value| value.to_string());
}

#[derive(Debug, Deserialize)]
pub struct SaveHistoryRequest {
    #[serde(default)]
    pub committed_operation_id: Option<String>,
    pub prompt: String,
    pub generated_content: String,
    pub doc_type: Option<String>,
    pub audience: Option<String>,
    pub reference_count: i64,
    #[serde(default)]
    pub references: Vec<serde_json::Value>,
    pub model: Option<String>,
    #[serde(default)]
    pub latency_ms: Option<i64>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub history_id: Option<i64>,
    #[serde(default)]
    pub conversation: Vec<serde_json::Value>,
    #[serde(default)]
    pub agent_trace: Vec<serde_json::Value>,
    #[serde(default)]
    pub goal: Option<serde_json::Value>,
    #[serde(default)]
    pub root_request: Option<String>,
    #[serde(default)]
    pub edit_operation: Option<String>,
    #[serde(default)]
    pub document_patch: Option<serde_json::Value>,
    #[serde(default)]
    pub evidence: Vec<serde_json::Value>,
    /// 记录来源：creation（缺省）/ scheduled_task。
    #[serde(default)]
    pub source_kind: Option<String>,
    #[serde(default)]
    pub source_ref_id: Option<i64>,
    /// 生命周期状态：running / completed / failed / cancelled。
    #[serde(default)]
    pub lifecycle_status: Option<String>,
    #[serde(default = "default_creation_mode")]
    pub creation_mode: String,
    #[serde(default)]
    pub creation_brief: Option<serde_json::Value>,
    #[serde(default)]
    pub brainstorm_revision: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct SaveHistoryResponse {
    pub id: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub progress_epoch: Option<i64>,
}

pub async fn save_history(
    State(state): State<Arc<AppState>>,
    Json(req): Json<SaveHistoryRequest>,
) -> Result<Json<SaveHistoryResponse>, (StatusCode, String)> {
    let id = state
        .storage
        .with_conn(|conn| {
            if let (Some(session),Some(operation_id)) = (req.session_id.as_deref(),req.committed_operation_id.as_deref()) {
                let operation = crate::storage::repo::creation_operation::get(conn,session,operation_id)?
                    .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
                if !matches!(operation.status.as_str(), "completed" | "partial") { return Err(rusqlite::Error::InvalidQuery.into()); }
                let history = crate::storage::repo::creation_history::get_by_id(conn,operation.history_id)?
                    .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
                let stored = history.conversation_json.as_deref()
                    .and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default();
                let conversation = merge_creation_history_conversation(stored,req.conversation.clone(),history.root_request.as_deref());
                // Acknowledgement may update metadata, never replay an already committed document.
                conn.execute("UPDATE creation_history SET conversation_json=?2 WHERE id=?1",
                    rusqlite::params![history.id,serde_json::to_string(&conversation)?])?;
                if operation.result.as_ref().and_then(|r|r["data"]["revision_no"].as_i64()) == Some(history.revision_no) {
                    conn.execute("UPDATE creation_history SET references_json=?2,reference_count=?3,
                        model=?4,latency_ms=?5,evidence_json=?6 WHERE id=?1",
                        rusqlite::params![history.id,serde_json::to_string(&req.references)?,req.reference_count,
                            req.model,req.latency_ms,serde_json::to_string(&req.evidence)?])?;
                    let ids: Vec<String> = req.evidence.iter().filter_map(|e|e["id"].as_str().map(str::to_string)).collect();
                    crate::storage::repo::creation_evidence::attach_to_history(conn,history.id,&ids)?;
                }
                return Ok(history.id);
            }
            let references_json = serde_json::to_string(&req.references)?;
            let evidence_ids = req
                .evidence
                .iter()
                .filter(|item| {
                    item.get("validation_status")
                        .and_then(|value| value.as_str())
                        == Some("verified")
                })
                .filter_map(|item| item.get("id").and_then(|value| value.as_str()))
                .map(ToOwned::to_owned)
                .collect::<Vec<_>>();
            let goal_json = req.goal.as_ref().map(serde_json::to_string).transpose()?;
            let session_id = req
                .session_id
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty());
            let session_prior = session_id
                .map(|session_id| {
                    crate::storage::repo::creation_history::get_session_context(conn, session_id)
                })
                .transpose()?
                .flatten();
            let resumed_existing_session = session_prior.is_some();
            let prior = if session_prior.is_some() {
                session_prior
            } else {
                req.history_id
                    .map(|history_id| {
                        crate::storage::repo::creation_history::get_by_id(conn, history_id)
                    })
                    .transpose()?
                    .flatten()
                    .filter(|latest| {
                        latest
                            .session_id
                            .as_deref()
                            .map(str::trim)
                            .filter(|value| !value.is_empty())
                            .is_none()
                    })
                    .map(|latest| {
                        let root_request = latest
                            .root_request
                            .as_deref()
                            .map(str::trim)
                            .filter(|value| !value.is_empty())
                            .map(ToOwned::to_owned)
                            .unwrap_or_else(|| latest.prompt.clone());
                        crate::storage::repo::creation_history::CreationSessionContext {
                            root_request,
                            latest,
                        }
                    })
            };
            let mut merged_evidence = prior
                .as_ref()
                .and_then(|context| context.latest.evidence_json.as_deref())
                .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
                .unwrap_or_default();
            for evidence in &req.evidence {
                let evidence_id = evidence.get("id").and_then(|value| value.as_str());
                if let Some(index) = evidence_id.and_then(|id| {
                    merged_evidence.iter().position(|existing| {
                        existing.get("id").and_then(|value| value.as_str()) == Some(id)
                    })
                }) {
                    merged_evidence[index] = evidence.clone();
                } else {
                    merged_evidence.push(evidence.clone());
                }
            }
            let evidence_json = serde_json::to_string(&merged_evidence)?;
            let root_request = req
                .root_request
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
                .or_else(|| prior.as_ref().map(|context| context.root_request.clone()))
                .or_else(|| conversation_root_request(&req.conversation))
                .unwrap_or_else(|| req.prompt.clone());
            let stored_conversation = prior
                .as_ref()
                .and_then(|context| context.latest.conversation_json.as_deref())
                .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
                .unwrap_or_default();
            let mut merged_conversation = merge_creation_history_conversation(
                stored_conversation.clone(),
                req.conversation.clone(),
                Some(&root_request),
            );
            ensure_current_creation_instruction(
                &mut merged_conversation,
                &stored_conversation,
                &req.prompt,
            );
            let conversation_json = serde_json::to_string(&merged_conversation)?;
            let stored_trace = prior
                .as_ref()
                .and_then(|context| context.latest.agent_trace_json.as_deref())
                .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
                .unwrap_or_default();
            let merged_trace = merge_creation_history_trace(stored_trace, req.agent_trace.clone());
            let agent_trace_json = serde_json::to_string(&merged_trace)?;
            let revision_no = if resumed_existing_session {
                crate::storage::repo::creation_history::next_revision_no(
                    conn,
                    session_id.unwrap_or_default(),
                )?
            } else {
                prior
                    .as_ref()
                    .map(|context| context.latest.revision_no + 1)
                    .unwrap_or(1)
            };
            let edit_operation = normalize_edit_operation(req.edit_operation.as_deref().unwrap_or(
                if prior.is_some() {
                    "rewrite_document"
                } else {
                    "create_document"
                },
            ));
            let document_patch_json = req
                .document_patch
                .as_ref()
                .map(serde_json::to_string)
                .transpose()?;
            let history_id = if let (Some(context), Some(session_id)) = (prior.as_ref(), session_id)
            {
                crate::storage::repo::creation_history::update_session(
                    conn,
                    context.latest.id,
                    &req.prompt,
                    &req.generated_content,
                    req.doc_type.as_deref(),
                    req.audience.as_deref(),
                    req.reference_count,
                    Some(&references_json),
                    req.model.as_deref(),
                    req.latency_ms,
                    session_id,
                    Some(&conversation_json),
                    Some(&agent_trace_json),
                    goal_json.as_deref(),
                    &root_request,
                    revision_no,
                    edit_operation,
                    document_patch_json.as_deref(),
                )?;
                context.latest.id
            } else {
                crate::storage::repo::creation_history::insert(
                    conn,
                    &req.prompt,
                    &req.generated_content,
                    req.doc_type.as_deref(),
                    req.audience.as_deref(),
                    req.reference_count,
                    Some(&references_json),
                    req.model.as_deref(),
                    req.latency_ms,
                    session_id,
                    Some(&conversation_json),
                    Some(&agent_trace_json),
                    goal_json.as_deref(),
                    Some(&root_request),
                    None,
                    revision_no,
                    edit_operation,
                    document_patch_json.as_deref(),
                )?
            };
            crate::storage::repo::creation_history::set_evidence_json(
                conn,
                history_id,
                &evidence_json,
            )?;
            crate::storage::repo::creation_evidence::attach_to_history(
                conn,
                history_id,
                &evidence_ids,
            )?;
            if let Some(source_kind) = normalize_history_source_kind(req.source_kind.as_deref()) {
                crate::storage::repo::creation_history::set_source(
                    conn,
                    history_id,
                    source_kind,
                    req.source_ref_id,
                )?;
            }
            crate::storage::repo::creation_history::set_lifecycle_status(
                conn,
                history_id,
                normalize_history_lifecycle_status(req.lifecycle_status.as_deref()),
            )?;
            let creation_brief_json = req
                .creation_brief
                .as_ref()
                .map(serde_json::to_string)
                .transpose()?;
            crate::storage::repo::creation_history::set_brainstorm_metadata(
                conn,
                history_id,
                if req.creation_mode == "brainstorm" {
                    "brainstorm"
                } else {
                    "direct"
                },
                creation_brief_json.as_deref(),
                req.brainstorm_revision,
            )?;
            Ok(history_id)
        })
        .map_err(|e| {
            error!("保存创作记录失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        })?;

    Ok(Json(SaveHistoryResponse {
        id,
        progress_epoch: None,
    }))
}

#[derive(Debug, Deserialize)]
pub struct StartHistoryRequest {
    pub prompt: String,
    pub session_id: String,
    #[serde(default)]
    pub generated_content: String,
    #[serde(default)]
    pub doc_type: Option<String>,
    #[serde(default)]
    pub audience: Option<String>,
    #[serde(default)]
    pub root_request: Option<String>,
    #[serde(default)]
    pub conversation: Vec<serde_json::Value>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default = "default_creation_mode")]
    pub creation_mode: String,
    #[serde(default)]
    pub creation_brief: Option<serde_json::Value>,
    #[serde(default)]
    pub brainstorm_revision: Option<i64>,
}

/// POST /api/creation/history/start - 创作一开始就建立或唤醒一条进行中记录。
pub async fn start_history(
    State(state): State<Arc<AppState>>,
    Json(req): Json<StartHistoryRequest>,
) -> Result<Json<SaveHistoryResponse>, (StatusCode, String)> {
    let session_id = req.session_id.trim();
    if session_id.is_empty() || req.prompt.trim().is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            "prompt 和 session_id 不能为空".to_string(),
        ));
    }
    let (id, progress_epoch) = state
        .storage
        .with_conn(|conn| {
            let creation_brief_json = req
                .creation_brief
                .as_ref()
                .map(serde_json::to_string)
                .transpose()?;
            let existing =
                crate::storage::repo::creation_history::get_session_context(conn, session_id)?;
            if let Some(context) = existing {
                let stored_conversation = context
                    .latest
                    .conversation_json
                    .as_deref()
                    .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
                    .unwrap_or_default();
                let merged_conversation = merge_creation_history_conversation(
                    stored_conversation,
                    req.conversation.clone(),
                    Some(&context.root_request),
                );
                let conversation_json = serde_json::to_string(&merged_conversation)?;
                let durable: bool = conn.query_row("SELECT EXISTS(SELECT 1 FROM creation_operations WHERE history_id=?1)",
                    rusqlite::params![context.latest.id], |row| row.get(0))?;
                let generated_content = if durable || (req.generated_content.trim().is_empty()
                    && !context.latest.generated_content.trim().is_empty())
                {
                    context.latest.generated_content.as_str()
                } else {
                    req.generated_content.as_str()
                };
                let progress_epoch = crate::storage::repo::creation_history::start_progress(
                    conn,
                    context.latest.id,
                    Some(generated_content),
                    Some(&conversation_json),
                )?
                .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
                crate::storage::repo::creation_history::set_brainstorm_metadata(
                    conn,
                    context.latest.id,
                    if req.creation_mode == "brainstorm" {
                        "brainstorm"
                    } else {
                        "direct"
                    },
                    creation_brief_json.as_deref(),
                    req.brainstorm_revision,
                )?;
                return Ok((context.latest.id, progress_epoch));
            }
            let root_request = req
                .root_request
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .unwrap_or(req.prompt.trim());
            let conversation_json = serde_json::to_string(&req.conversation)?;
            let history_id = crate::storage::repo::creation_history::insert(
                conn,
                req.prompt.trim(),
                &req.generated_content,
                req.doc_type.as_deref(),
                req.audience.as_deref(),
                0,
                Some("[]"),
                req.model.as_deref(),
                None,
                Some(session_id),
                Some(&conversation_json),
                Some("[]"),
                None,
                Some(root_request),
                None,
                1,
                "create_document",
                None,
            )?;
            let progress_epoch = crate::storage::repo::creation_history::start_progress(
                conn, history_id, None, None,
            )?
            .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
            crate::storage::repo::creation_history::set_brainstorm_metadata(
                conn,
                history_id,
                if req.creation_mode == "brainstorm" {
                    "brainstorm"
                } else {
                    "direct"
                },
                creation_brief_json.as_deref(),
                req.brainstorm_revision,
            )?;
            Ok((history_id, progress_epoch))
        })
        .map_err(|e| {
            error!("建立进行中创作记录失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        })?;
    Ok(Json(SaveHistoryResponse {
        id,
        progress_epoch: Some(progress_epoch),
    }))
}

#[derive(Debug, Deserialize)]
pub struct UpdateHistoryProgressRequest {
    pub lifecycle_status: String,
    #[serde(default)]
    pub progress_epoch: Option<i64>,
    #[serde(default)]
    pub generated_content: Option<String>,
    #[serde(default)]
    pub conversation: Option<Vec<serde_json::Value>>,
    #[serde(default)]
    pub agent_trace: Option<Vec<serde_json::Value>>,
    #[serde(default)]
    pub latency_ms: Option<i64>,
}

/// PATCH /api/creation/history/:id/progress - 保存后台执行的最新现场与终态。
pub async fn update_history_progress(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    Json(req): Json<UpdateHistoryProgressRequest>,
) -> Result<StatusCode, (StatusCode, String)> {
    let status = normalize_history_lifecycle_status(Some(&req.lifecycle_status));
    let changed = state
        .storage
        .with_conn(|conn| {
            let existing = crate::storage::repo::creation_history::get_by_id(conn, id)?;
            let conversation_json = if let Some(current) = req.conversation.as_ref() {
                let stored = existing
                    .as_ref()
                    .and_then(|history| history.conversation_json.as_deref())
                    .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
                    .unwrap_or_default();
                Some(serde_json::to_string(
                    &merge_creation_history_conversation(
                        stored,
                        current.clone(),
                        existing
                            .as_ref()
                            .and_then(|history| history.root_request.as_deref()),
                    ),
                )?)
            } else {
                None
            };
            let agent_trace_json = if let Some(current) = req.agent_trace.as_ref() {
                let stored = existing
                    .as_ref()
                    .and_then(|history| history.agent_trace_json.as_deref())
                    .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
                    .unwrap_or_default();
                Some(serde_json::to_string(&merge_creation_history_trace(
                    stored,
                    current.clone(),
                ))?)
            } else {
                None
            };
            let durable: bool = conn.query_row("SELECT EXISTS(SELECT 1 FROM creation_operations WHERE history_id=?1)",
                rusqlite::params![id], |row| row.get(0))?;
            let terminal_operation: bool = if durable {
                conn.query_row("SELECT COALESCE((SELECT status IN ('completed','partial') FROM creation_operations WHERE history_id=?1 ORDER BY updated_at DESC,created_at DESC LIMIT 1),0)",rusqlite::params![id],|row|row.get(0))?
            } else { false };
            let effective_status = if terminal_operation {
                existing.as_ref().map(|h|h.lifecycle_status.as_str()).unwrap_or(status)
            } else {status};
            crate::storage::repo::creation_history::update_progress(
                conn,
                id,
                effective_status,
                if durable { None } else { req.generated_content.as_deref() },
                conversation_json.as_deref(),
                if durable { None } else { agent_trace_json.as_deref() },
                req.latency_ms,
                req.progress_epoch,
            )
            .map_err(Into::into)
        })
        .map_err(|e| {
            error!("更新创作进度失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        })?;
    if changed {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Err((StatusCode::NOT_FOUND, "创作记录不存在".to_string()))
    }
}

#[derive(Debug, Deserialize)]
pub struct ListHistoryParams {
    #[serde(default)]
    pub limit: Option<usize>,
    #[serde(default)]
    pub offset: usize,
    #[serde(default)]
    pub q: Option<String>,
    #[serde(default)]
    pub paged: bool,
}

#[derive(Debug, Serialize)]
pub struct HistoryPageResponse {
    pub items: Vec<crate::storage::repo::creation_history::CreationHistory>,
    pub total: usize,
    pub limit: usize,
    pub offset: usize,
}

pub async fn list_history(
    State(state): State<Arc<AppState>>,
    Query(params): Query<ListHistoryParams>,
) -> Result<Response, (StatusCode, String)> {
    let limit = params
        .limit
        .unwrap_or(if params.paged { 20 } else { 50 })
        .clamp(1, 100);
    let offset = params.offset.min(1_000_000);
    let query = params
        .q
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    let now = chrono::Utc::now().timestamp_millis();
    let active_sessions = {
        let mut leases = state
            .creation_session_leases
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        leases.retain(|_, lease| now - lease.updated_at_ms <= CREATION_LEASE_TTL_MS);
        leases
            .keys()
            .cloned()
            .collect::<std::collections::HashSet<_>>()
    };
    let (histories, total) = state
        .storage
        .with_conn(|conn| {
            let (settled_histories, settled_operations) =
                crate::storage::repo::creation_history::settle_stale_running(
                    conn,
                    now.saturating_sub(CREATION_LEASE_TTL_MS),
                    &active_sessions,
                )?;
            if settled_histories > 0 || settled_operations > 0 {
                info!(
                    settled_histories,
                    settled_operations, "已收口无法继续运行的历史创作状态"
                );
            }
            crate::storage::repo::creation_history::list_page(conn, query.as_deref(), limit, offset)
                .map_err(Into::into)
        })
        .map_err(|e| {
            error!("查询创作记录失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        })?;

    if params.paged {
        Ok(Json(HistoryPageResponse {
            items: histories,
            total,
            limit,
            offset,
        })
        .into_response())
    } else {
        // 保留旧客户端依赖的数组响应；新页面显式传 paged=true 获取分页元数据。
        Ok(Json(histories).into_response())
    }
}

/// 归一化创作记录来源，非法值回退默认 creation。
fn normalize_history_source_kind(value: Option<&str>) -> Option<&'static str> {
    let value = value.map(str::trim).filter(|value| !value.is_empty())?;
    match value {
        "scheduled_task" => Some("scheduled_task"),
        _ => Some("creation"),
    }
}

fn normalize_history_lifecycle_status(value: Option<&str>) -> &'static str {
    match value.map(str::trim) {
        Some("running") => "running",
        Some("failed") => "failed",
        Some("cancelled") => "cancelled",
        _ => "completed",
    }
}

/// GET /api/creation/history/:id - 获取单条创作记录（供任务页跳转恢复）
pub async fn get_history(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Response, (StatusCode, String)> {
    let history = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_history::get_by_id(conn, id).map_err(Into::into)
        })
        .map_err(|e| {
            error!("查询创作记录失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        })?;
    match history {
        Some(history) => Ok(Json(history).into_response()),
        None => Err((StatusCode::NOT_FOUND, "创作记录不存在".to_string())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn brainstorm_diagnostics_only_keep_bounded_identifiers_and_known_actions() {
        assert_eq!(
            brainstorm_log_identifier("creation-1788802278839-b615a62dd42728"),
            "creation-1788802278839-b615a62dd42728"
        );
        for unsafe_value in [
            "", "raw user prompt", "https://internal.example.com",
            "session\nsecret", &"a".repeat(161),
        ] {
            assert_eq!(brainstorm_log_identifier(unsafe_value), "invalid");
        }
        assert_eq!(brainstorm_log_action("start"), "start");
        assert_eq!(brainstorm_log_action("raw_user_prompt"), "invalid");
    }

    #[test]
    fn brainstorm_sidecar_errors_use_stable_safe_contracts() {
        let cases = [
            ("BRAINSTORM_MODEL_OUTPUT_TRUNCATED", "BRAINSTORM_MODEL_OUTPUT_TRUNCATED", StatusCode::BAD_GATEWAY, false),
            ("CREATION_DOCUMENT_TRUNCATED", "BRAINSTORM_MODEL_OUTPUT_TRUNCATED", StatusCode::BAD_GATEWAY, false),
            ("BRAINSTORM_MODEL_OUTPUT_INVALID", "BRAINSTORM_MODEL_OUTPUT_INVALID", StatusCode::BAD_GATEWAY, false),
            ("MODEL_ACCESS_DENIED", "MODEL_ACCESS_DENIED", StatusCode::FORBIDDEN, false),
            ("MODEL_RATE_LIMITED", "MODEL_RATE_LIMITED", StatusCode::TOO_MANY_REQUESTS, true),
            ("MODEL_REQUEST_FAILED", "MODEL_REQUEST_FAILED", StatusCode::BAD_GATEWAY, false),
            ("MODEL_TRANSPORT_UNAVAILABLE", "BRAINSTORM_MODEL_UNAVAILABLE", StatusCode::SERVICE_UNAVAILABLE, true),
            ("MODEL_SERVICE_UNAVAILABLE", "BRAINSTORM_MODEL_UNAVAILABLE", StatusCode::SERVICE_UNAVAILABLE, true),
            ("CREATION_UNAVAILABLE", "BRAINSTORM_MODEL_UNAVAILABLE", StatusCode::SERVICE_UNAVAILABLE, true),
            ("BRAINSTORM_MODEL_UNAVAILABLE", "BRAINSTORM_MODEL_UNAVAILABLE", StatusCode::SERVICE_UNAVAILABLE, true),
            ("BRAINSTORM_MODEL_TIMEOUT", "BRAINSTORM_MODEL_TIMEOUT", StatusCode::GATEWAY_TIMEOUT, true),
        ];
        for (upstream_code, expected_code, expected_status, retryable) in cases {
            let detail = serde_json::json!({
                "code": upstream_code,
                "message": "provider secret=sk-private at https://internal.example.com; raw user prompt",
                "retryable": !retryable,
            });
            for body in [detail.clone(), serde_json::json!({"detail": detail})] {
                // The stable category wins even when the old sidecar used 503 for every failure.
                let (status, Json(body)) = brainstorm_sidecar_error(StatusCode::SERVICE_UNAVAILABLE, &body);
                assert_eq!(status, expected_status, "{upstream_code}");
                assert_eq!(body["code"], expected_code);
                assert_eq!(body["retryable"], retryable);
                assert!(body["message"].as_str().unwrap().chars().all(|c| !c.is_ascii_alphabetic()));
                assert!(!body.to_string().contains("sk-private"));
                assert!(!body.to_string().contains("internal.example.com"));
                assert!(!body.to_string().contains("raw user prompt"));
            }
        }
    }

    #[test]
    fn brainstorm_unknown_errors_fall_back_without_exposing_upstream_content() {
        for body in [
            serde_json::json!({"detail":{"code":"PROVIDER_SECRET", "message":"private"}}),
            serde_json::json!({"detail":"provider traceback secret"}),
            serde_json::Value::Null,
        ] {
            for (upstream_status, expected_code) in [
                (StatusCode::BAD_GATEWAY, "BRAINSTORM_MODEL_OUTPUT_INVALID"),
                (StatusCode::TOO_MANY_REQUESTS, "MODEL_RATE_LIMITED"),
                (StatusCode::SERVICE_UNAVAILABLE, "BRAINSTORM_MODEL_UNAVAILABLE"),
                (StatusCode::GATEWAY_TIMEOUT, "BRAINSTORM_MODEL_TIMEOUT"),
            ] {
                let (_, Json(failure)) = brainstorm_sidecar_error(upstream_status, &body);
                assert_eq!(failure["code"], expected_code);
                assert!(!failure.to_string().contains("private"));
                assert!(!failure.to_string().contains("secret"));
            }
        }
    }

    #[test]
    fn inline_user_instruction_keeps_requirement_selection_and_polish_detail() {
        let instruction =
            inline_user_instruction("elaborate", "模型承担主要生产负载，\n占比95%。", "");

        assert!(instruction.contains("细化要求：细化所选内容"));
        assert!(instruction.contains("选取内容：模型承担主要生产负载， 占比95%。"));
        let polish = inline_user_instruction("polish", "原始表达", "更专业，但不要有官话");
        assert!(polish.contains("补充要求：更专业，但不要有官话"));
        let brainstorm =
            inline_user_instruction("brainstorm", "项目将分阶段推进", "实施节奏：先试点再推广");
        assert!(brainstorm.contains("脑暴写回要求：按本轮已确认的局部脑暴结论"));
        assert!(brainstorm.contains("补充要求：实施节奏：先试点再推广"));
        assert_eq!(inline_operation("brainstorm"), Some("brainstorm_selection"));
    }

    fn test_brainstorm_question(question_type: &str, allow_custom: bool) -> BrainstormQuestion {
        BrainstormQuestion {
            exploration_stage: None,
            parent_question_id: None,
            parent_option_id: None,
            single_choice_reason: String::new(),
            id: "question-1".to_string(),
            dimension_id: "dimension-1".to_string(),
            dimension: "测试维度".to_string(),
            question_type: question_type.to_string(),
            prompt: "请选择答案".to_string(),
            why_now: "用于验证答案契约".to_string(),
            context_details: String::new(),
            required: true,
            allow_custom,
            options: vec![
                BrainstormOption {
                    id: "recommended".to_string(),
                    label: "推荐答案".to_string(),
                    description: "推荐方向的依据与取舍".to_string(),
                    details: String::new(),
                    recommended: true,
                },
                BrainstormOption {
                    id: "alternative".to_string(),
                    label: "备选答案".to_string(),
                    description: "备选方向的依据与取舍".to_string(),
                    details: String::new(),
                    recommended: false,
                },
            ],
            answer_template: String::new(),
        }
    }

    fn test_brainstorm_answer(selected_option_ids: &[&str], custom_text: &str) -> BrainstormAnswer {
        BrainstormAnswer {
            selected_option_ids: selected_option_ids
                .iter()
                .map(|id| (*id).to_string())
                .collect(),
            custom_text: custom_text.to_string(),
            source: "user".to_string(),
        }
    }

    #[test]
    fn brainstorm_legacy_display_fields_restore_without_truncating_original_content() {
        let mut question = serde_json::to_value(test_brainstorm_question("multi_choice", true)).unwrap();
        question.as_object_mut().unwrap().remove("context_details");
        let original_description = "旧会话完整选项和引用资料".repeat(80);
        for option in question["options"].as_array_mut().unwrap() {
            option.as_object_mut().unwrap().remove("details");
            option["description"] = serde_json::json!(original_description);
        }
        let stored = parse_brainstorm_state(&serde_json::json!({
            "root_request": "原始需求", "current_question": question,
            "continuation_directions": [{"id": "more", "label": "补充", "description": "旧方向说明"}]
        }).to_string()).unwrap();
        let restored = stored.current_question.unwrap();
        assert!(restored.context_details.is_empty());
        assert!(restored.options.iter().all(|option| option.details.is_empty()));
        assert_eq!(restored.options[0].description, original_description);
        assert!(stored.continuation_directions[0].details.is_empty());
    }

    #[test]
    fn brainstorm_display_details_roundtrip_in_current_history_archive_and_directions() {
        let mut question = test_brainstorm_question("multi_choice", true);
        question.context_details = "本轮已检索历史记忆，来源原文可供核对。".to_string();
        question.options[0].details = "《项目决定》 · document:1「优先降低审核成本」".to_string();
        let mut current = question.clone();
        current.id = "next-question".to_string();
        current.prompt = "如何安排审核步骤？".to_string();
        let state_json = serde_json::json!({
            "root_request": "原始需求", "memory_brief": "创作使用的完整历史证据",
            "current_question": current,
            "turns": [{"question": question, "answer": test_brainstorm_answer(&["recommended"], "")}],
            "archived_questions": [{"question": question, "answer": null}],
            "continuation_directions": [{"id": "more", "label": "继续", "description": "探索实施节奏",
                "details": "该方向仍待用户确认。", "recommended": true}]
        }).to_string();
        let stored = parse_brainstorm_state(&state_json).unwrap();
        let restored = parse_brainstorm_state(&serde_json::to_string(&stored).unwrap()).unwrap();
        assert_eq!(restored.memory_brief, "创作使用的完整历史证据");
        for restored_question in [
            restored.current_question.as_ref().unwrap(),
            &restored.turns[0].question,
            &restored.archived_questions[0].question,
        ] {
            assert_eq!(restored_question.context_details, question.context_details);
            assert_eq!(restored_question.options[0].details, question.options[0].details);
            assert_eq!(restored_question.options[0].description, question.options[0].description);
        }
        let direction = &restored.continuation_directions[0];
        let option = BrainstormOption::from(direction);
        assert_eq!(option.details, "该方向仍待用户确认。");
        assert_eq!(option.description, direction.description);
        assert_eq!(option.id, direction.id);
        assert_eq!(option.label, direction.label);
        assert_eq!(option.recommended, direction.recommended);
        assert_eq!(restored.turns[0].answer.selected_option_ids, vec!["recommended"]);
    }

    #[test]
    fn choice_answers_require_options_or_custom_text_but_never_both() {
        let single_choice = test_brainstorm_question("single_choice", true);
        assert!(validate_brainstorm_answer(
            &single_choice,
            &test_brainstorm_answer(&["recommended"], ""),
        ));
        assert!(validate_brainstorm_answer(
            &single_choice,
            &test_brainstorm_answer(&[], "自定义答案"),
        ));
        assert!(!validate_brainstorm_answer(
            &single_choice,
            &test_brainstorm_answer(&[], "   "),
        ));
        assert!(!validate_brainstorm_answer(
            &single_choice,
            &test_brainstorm_answer(&["recommended"], "补充说明"),
        ));

        let multi_choice = test_brainstorm_question("multi_choice", true);
        assert!(validate_brainstorm_answer(
            &multi_choice,
            &test_brainstorm_answer(&["recommended", "alternative"], ""),
        ));
        assert!(validate_brainstorm_answer(
            &multi_choice,
            &test_brainstorm_answer(&[], "自定义多选答案"),
        ));
        assert!(!validate_brainstorm_answer(
            &multi_choice,
            &test_brainstorm_answer(&["recommended", "alternative"], "补充说明"),
        ));

        let custom_disabled = test_brainstorm_question("single_choice", false);
        assert!(validate_brainstorm_answer(
            &custom_disabled,
            &test_brainstorm_answer(&["recommended"], ""),
        ));
        assert!(!validate_brainstorm_answer(
            &custom_disabled,
            &test_brainstorm_answer(&[], "不允许的自定义答案"),
        ));
    }

    #[test]
    fn legacy_confirm_inference_answers_reject_option_ids() {
        let legacy_question = test_brainstorm_question("confirm_inference", false);
        assert!(validate_brainstorm_answer(
            &legacy_question,
            &test_brainstorm_answer(&[], "自由文本答案"),
        ));
        assert!(!validate_brainstorm_answer(
            &legacy_question,
            &test_brainstorm_answer(&["recommended"], "自由文本答案"),
        ));
    }

    #[test]
    fn generated_questions_require_enumerated_options_and_custom_entry() {
        let valid = test_brainstorm_question("single_choice", true);
        assert!(is_enumerated_brainstorm_question(&valid));

        let mut free_text = valid.clone();
        free_text.question_type = "free_text".to_string();
        free_text.options.clear();
        assert!(!is_enumerated_brainstorm_question(&free_text));

        let mut custom_disabled = valid.clone();
        custom_disabled.allow_custom = false;
        assert!(!is_enumerated_brainstorm_question(&custom_disabled));

        let mut duplicate_recommendations = valid;
        duplicate_recommendations.options[1].recommended = true;
        assert!(!is_enumerated_brainstorm_question(
            &duplicate_recommendations
        ));
    }

    #[test]
    fn start_history_request_accepts_ready_brainstorm_metadata() {
        let request: StartHistoryRequest = serde_json::from_value(serde_json::json!({
            "prompt": "设计数据治理平台方案",
            "session_id": "session-brainstorm-ready",
            "creation_mode": "brainstorm",
            "creation_brief": {
                "phase": "ready",
                "revision": 4
            },
            "brainstorm_revision": 4
        }))
        .unwrap();

        assert_eq!(request.creation_mode, "brainstorm");
        assert_eq!(request.brainstorm_revision, Some(4));
        assert_eq!(request.creation_brief.unwrap()["phase"], "ready");
    }

    #[test]
    fn merges_persisted_root_with_latest_conversation_without_duplicates() {
        let stored = vec![
            serde_json::json!({"role": "user", "content": "生成新能源行业方案", "id": "old-1"}),
            serde_json::json!({"role": "assistant", "content": "已生成首版"}),
        ];
        let current = vec![
            serde_json::json!({"role": "assistant", "content": "已生成首版"}),
            serde_json::json!({"role": "user", "content": "补充行业调研"}),
        ];

        let merged = merge_creation_conversation(stored, current, Some("生成新能源行业方案"));

        assert_eq!(merged.len(), 3);
        assert_eq!(
            conversation_root_request(&merged).as_deref(),
            Some("生成新能源行业方案")
        );
        assert_eq!(merged[2]["content"], "补充行业调研");
        assert!(merged.iter().all(|item| item.get("id").is_none()));
    }

    #[test]
    fn keeps_root_and_recent_turns_when_conversation_is_large() {
        let current = (0..80)
            .map(|index| {
                serde_json::json!({
                    "role": if index % 2 == 0 { "user" } else { "assistant" },
                    "content": format!("第 {index} 条")
                })
            })
            .collect();
        let merged = merge_creation_conversation(Vec::new(), current, Some("最初的完整需求"));

        assert_eq!(merged.len(), 60);
        assert_eq!(merged[0]["content"], "最初的完整需求");
        assert_eq!(merged.last().unwrap()["content"], "第 79 条");
    }

    #[test]
    fn preserves_a_repeated_instruction_when_it_is_a_new_occurrence() {
        let stored = vec![
            serde_json::json!({"role": "user", "content": "生成方案"}),
            serde_json::json!({"role": "user", "content": "继续完善"}),
        ];
        let current = vec![
            serde_json::json!({"role": "user", "content": "生成方案"}),
            serde_json::json!({"role": "user", "content": "继续完善"}),
            serde_json::json!({"role": "user", "content": "继续完善"}),
        ];

        let merged = merge_creation_conversation(stored, current, Some("生成方案"));
        let repeated = merged
            .iter()
            .filter(|item| item["content"] == "继续完善")
            .count();

        assert_eq!(repeated, 2);
    }

    #[test]
    fn history_merge_preserves_message_metadata_and_restores_a_missing_turn() {
        let stored = vec![
            serde_json::json!({
                "id": "user-1",
                "role": "user",
                "content": "写一份周年员工的礼物指南",
                "runIds": ["run-1"]
            }),
            serde_json::json!({
                "id": "assistant-1",
                "role": "assistant",
                "content": "文档已更新",
                "runId": "run-1"
            }),
        ];
        let current = vec![
            stored[0].clone(),
            stored[1].clone(),
            serde_json::json!({
                "id": "assistant-2",
                "role": "assistant",
                "content": "已完成 72 处调整",
                "runId": "run-2"
            }),
        ];

        let mut merged = merge_creation_history_conversation(
            stored.clone(),
            current,
            Some("写一份周年员工的礼物指南"),
        );
        ensure_current_creation_instruction(&mut merged, &stored, "参考示例公司员工周年礼物方案");

        assert_eq!(merged[0]["id"], "user-1");
        assert_eq!(merged[0]["runIds"][0], "run-1");
        assert_eq!(merged[2]["content"], "参考示例公司员工周年礼物方案");
        assert!(merged[2]["id"]
            .as_str()
            .is_some_and(|value| value.starts_with("server-user-")));
        assert_eq!(merged[3]["runId"], "run-2");
    }

    #[test]
    fn history_merge_keeps_repeated_current_instruction_occurrences() {
        let stored = vec![
            serde_json::json!({"role": "user", "content": "生成方案"}),
            serde_json::json!({"role": "user", "content": "继续完善"}),
        ];
        let mut merged =
            merge_creation_history_conversation(stored.clone(), stored.clone(), Some("生成方案"));

        ensure_current_creation_instruction(&mut merged, &stored, "继续完善");

        assert_eq!(user_instruction_count(&merged, "继续完善"), 2);
    }

    #[test]
    fn history_trace_merge_keeps_earlier_events_when_snapshot_only_has_latest_run() {
        let stored = vec![
            serde_json::json!({"event_id": "run-1-start", "type": "run.started"}),
            serde_json::json!({"event_id": "run-1-done", "type": "run.completed"}),
        ];
        let current = vec![serde_json::json!({"event_id": "run-2-start", "type": "run.started"})];

        let merged = merge_creation_history_trace(stored, current);

        assert_eq!(merged.len(), 3);
        assert_eq!(merged[0]["event_id"], "run-1-start");
        assert_eq!(merged[2]["event_id"], "run-2-start");
    }

    #[test]
    fn preserves_multi_section_revision_operation_for_history() {
        assert_eq!(
            normalize_edit_operation("revise_document"),
            "revise_document"
        );
    }

    #[test]
    fn relays_sse_when_a_chinese_character_crosses_network_chunks() {
        let payload =
            "data: {\"type\":\"run.completed\",\"summary\":\"已生成文档\"}\n\n".as_bytes();
        let chinese_start = payload
            .windows("已".len())
            .position(|window| window == "已".as_bytes())
            .unwrap();
        let split_at = chinese_start + 1;
        let mut buffer = Vec::new();

        assert!(append_sse_chunk(&mut buffer, &payload[..split_at]).is_empty());
        let events = append_sse_chunk(&mut buffer, &payload[split_at..]);

        assert_eq!(
            events,
            vec!["{\"type\":\"run.completed\",\"summary\":\"已生成文档\"}"]
        );
        assert!(buffer.is_empty());
    }

    #[test]
    fn creation_tool_contract_keeps_required_tools_enabled() {
        let request: GenerateRequest = serde_json::from_value(serde_json::json!({
            "user_prompt": "生成架构方案",
            "enabled_tools": ["plantuml_diagram", "memory_search", ""]
        }))
        .unwrap();

        assert_eq!(request.max_references, 10);
        assert_eq!(request.data_search_limit, 30);

        assert_eq!(
            normalize_creation_tool_ids(request.enabled_tools),
            vec![
                "internet_search".to_string(),
                "memory_search".to_string(),
                "data_search".to_string(),
                "webpage_scrape".to_string(),
                "plantuml_diagram".to_string(),
            ]
        );
    }

    #[test]
    fn creation_tool_contract_enables_mermaid_only_for_default_requests() {
        let request: GenerateRequest = serde_json::from_value(serde_json::json!({
            "user_prompt": "整理一份关系复杂的说明"
        }))
        .unwrap();

        assert_eq!(
            request.enabled_tools,
            vec![
                "internet_search".to_string(),
                "memory_search".to_string(),
                "data_search".to_string(),
                "webpage_scrape".to_string(),
                "mermaid_diagram".to_string(),
            ]
        );
        assert!(!normalize_creation_tool_ids(vec![])
            .iter()
            .any(|item| item == "mermaid_diagram"));
    }

    #[test]
    fn upgrades_fixed_question_state_into_dynamic_decision_path() {
        let legacy = serde_json::json!({
            "root_request": "设计企业知识库方案",
            "question_index": 2,
            "answers": {
                "outcome.primary": {
                    "selected_option_ids": ["align"],
                    "custom_text": "",
                    "source": "user"
                },
                "audience.primary": {
                    "selected_option_ids": ["technical"],
                    "custom_text": "研发与产品共同评审",
                    "source": "user"
                }
            },
            "invalidated_question_ids": []
        })
        .to_string();

        let upgraded = parse_brainstorm_state(&legacy).unwrap();

        assert_eq!(upgraded.turns.len(), 2);
        assert!(brainstorm_brief(&upgraded).contains("统一认知与方案方向"));
        assert!(brainstorm_brief(&upgraded).contains("研发与产品共同评审"));
        assert!(upgraded
            .turns
            .iter()
            .all(|turn| turn.question.question_type == "confirm_inference"));
        assert!(upgraded.current_question.is_none());
    }

    #[test]
    fn removes_legacy_free_text_current_question_for_regeneration() {
        let state = serde_json::json!({
            "root_request": "设计原创剧本生成方案",
            "turns": [],
            "current_question": {
                "id": "legacy-free-text",
                "dimension_id": "solution_architecture",
                "dimension": "总体方案与能力架构",
                "type": "free_text",
                "prompt": "采用哪种方向？",
                "why_now": "需要确认方向。",
                "required": true,
                "allow_custom": true,
                "options": [],
                "answer_template": "补充方向"
            }
        })
        .to_string();

        let upgraded = parse_brainstorm_state(&state).unwrap();

        assert!(upgraded.current_question.is_none());
    }

    #[test]
    fn removes_questions_reused_under_different_dimension_ids() {
        let repeated_prompt = "剧本生成结果应通过何种机制配置维护、审核发布及异常处理？";
        let mut ownership = test_brainstorm_question("single_choice", true);
        ownership.id = "ownership-question".to_string();
        ownership.dimension_id = "ownership_delivery".to_string();
        ownership.prompt = repeated_prompt.to_string();
        let mut rollout = ownership.clone();
        rollout.id = "rollout-question".to_string();
        rollout.dimension_id = "delivery_rollout".to_string();
        rollout.prompt = "剧本生成结果应通过何种机制配置维护、审核发布及异常处理 ？".to_string();
        let mut success = ownership.clone();
        success.id = "success-question".to_string();
        success.dimension_id = "success_criteria".to_string();

        let state = BrainstormStoredState {
            pending_extensions: Vec::new(),
            current_extension_id: None,
            prefetch_safe_options: Default::default(),
            branch_parent_revisions: Default::default(),
            memory_brief: String::new(),
            archived_questions: Vec::new(),
            brief_edits: Default::default(),
            root_request: "设计原创剧本生成方案".to_string(),
            user_input_revisions: Default::default(),
            selected_skills: Vec::new(),
            turns: vec![
                BrainstormStoredTurn {
                    question: ownership,
                    answer: test_brainstorm_answer(&["recommended"], ""),
                },
                BrainstormStoredTurn {
                    question: rollout,
                    answer: test_brainstorm_answer(&["recommended"], ""),
                },
            ],
            current_question: Some(success),
            open_flags: Vec::new(),
            readiness_reason: String::new(),
            continuation_directions: Vec::new(),
            invalidated_question_ids: Vec::new(),
        };

        let upgraded = parse_brainstorm_state(&serde_json::to_string(&state).unwrap()).unwrap();

        assert_eq!(upgraded.turns.len(), 1);
        assert_eq!(
            upgraded.turns[0].question.dimension_id,
            "ownership_delivery"
        );
        assert!(upgraded.current_question.is_none());
        assert_eq!(
            upgraded.invalidated_question_ids,
            vec!["rollout-question", "success-question"]
        );
    }

    #[test]
    fn brainstorm_source_revision_migration_is_persisted_once() {
        let mut stored: BrainstormStoredState = serde_json::from_value(serde_json::json!({
            "root_request": "旧目标", "brief_edits": {"root_request": "不检索个人资料", "open_flags": "用户约束"},
            "turns": [{"question": test_brainstorm_question("multi_choice", true),
                       "answer": {"selected_option_ids": [], "custom_text": "可以检索个人资料", "source": "user"}}]
        })).unwrap();
        let question_id = stored.turns[0].question.id.clone();
        initialize_brainstorm_input_revisions(&mut stored, 5);
        assert_eq!(stored.user_input_revisions["root_request"], 5);
        assert_eq!(stored.user_input_revisions["open_flags"], 5);
        assert_eq!(stored.user_input_revisions[&question_id], 1);
        let mut restored = parse_brainstorm_state(&serde_json::to_string(&stored).unwrap()).unwrap();
        initialize_brainstorm_input_revisions(&mut restored, 9);
        assert_eq!(restored.user_input_revisions, stored.user_input_revisions);
        restored.user_input_revisions.insert(question_id.clone(), 10);
        initialize_brainstorm_input_revisions(&mut restored, 11);
        assert_eq!(restored.user_input_revisions[&question_id], 10);
        assert_eq!(restored.user_input_revisions["root_request"], 5);
    }

    #[test]
    fn creation_checkpoint_requires_unchanged_explicit_source_inputs() {
        let brief = serde_json::json!({"revision": 1, "brief_edits": {"root_request": "可以检索个人资料"}});
        let checkpoint = serde_json::json!({"root_request":"组织读书会", "environment":{"creation_brief":brief}});
        let mut req: AgentRunRequest = serde_json::from_value(serde_json::json!({
            "user_prompt":"开始写作", "root_request":"组织读书会", "creation_brief":brief
        })).unwrap();
        assert!(creation_checkpoint_inputs_match(&req, &checkpoint));
        req.creation_brief = Some(serde_json::json!({"revision":2, "brief_edits":{"root_request":"不检索个人资料"}}));
        assert!(!creation_checkpoint_inputs_match(&req, &checkpoint));
        req.creation_brief = Some(serde_json::json!({}));
        assert!(!creation_checkpoint_inputs_match(&req, &checkpoint));
        assert!(creation_checkpoint_inputs_match(&req, &serde_json::json!({"environment":{}})));
        assert!(creation_checkpoint_inputs_match(&req, &serde_json::json!({"environment":{"creation_brief":{}}})));
        req.creation_brief = None;
        assert!(creation_checkpoint_inputs_match(&req, &checkpoint));
        req.root_request = Some("用户更改创作目标".to_string());
        assert!(!creation_checkpoint_inputs_match(&req, &checkpoint));
    }

    fn answer_pending_brainstorm_branch(
        stored: &mut BrainstormStoredState,
        id: &str,
        selected: &[&str],
        custom: &str,
    ) {
        let branch = pending_brainstorm_branch(stored).unwrap();
        let mut question = test_brainstorm_question("multi_choice", true);
        question.id = id.to_string();
        assert!(apply_brainstorm_question_stage(&mut question, Some(&branch)));
        if let Some(revision) = stored.user_input_revisions.get(&branch.question.id) {
            stored.branch_parent_revisions.insert(question.id.clone(), *revision);
        }
        stored.turns.push(BrainstormStoredTurn {
            question,
            answer: test_brainstorm_answer(selected, custom),
        });
    }

    #[test]
    fn brainstorm_branch_queue_visits_each_layer_before_descendants() {
        let mut stored: BrainstormStoredState =
            serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap();
        let root = test_brainstorm_question("multi_choice", true);
        stored.turns.push(BrainstormStoredTurn {
            question: root.clone(),
            answer: test_brainstorm_answer(&["alternative", "recommended"], ""),
        });
        let steps = [
            ("solution-a", root.id.as_str(), "recommended", "solutions", vec!["recommended", "alternative"]),
            ("solution-b", root.id.as_str(), "alternative", "solutions", vec!["recommended"]),
            ("delivery-a1", "solution-a", "recommended", "implementation", vec!["recommended"]),
            ("delivery-a2", "solution-a", "alternative", "implementation", vec!["recommended"]),
            ("delivery-b", "solution-b", "recommended", "implementation", vec!["recommended"]),
            ("validation-a1", "delivery-a1", "recommended", "validation", vec!["recommended", "alternative"]),
            ("validation-a2", "delivery-a2", "recommended", "validation", vec!["recommended"]),
            ("validation-b", "delivery-b", "recommended", "validation", vec!["recommended"]),
        ];
        for (id, parent, option, stage, selected) in steps {
            let branch = pending_brainstorm_branch(&stored).unwrap();
            assert_eq!(branch.question.id, parent);
            assert_eq!(branch.option.unwrap().id, option);
            assert_eq!(branch.exploration_stage, stage);
            answer_pending_brainstorm_branch(&mut stored, id, &selected, "");
        }
        assert!(pending_brainstorm_branch(&stored).is_none());
        assert_eq!(stored.turns.len(), 9);
    }

    #[test]
    fn brainstorm_parallel_sibling_rewording_is_rechecked_without_merging_different_branches() {
        let mut first = test_brainstorm_question("multi_choice", true);
        first.parent_question_id = Some("root".into());
        first.parent_option_id = Some("chosen".into());
        first.prompt = "小组交流中如何破冰让新人开口？".into();
        let mut repeated = first.clone();
        repeated.prompt = "如何设计破冰让新人愿意开口？".into();
        assert!(brainstorm_questions_overlap(&first, &repeated));
        repeated.parent_option_id = Some("another".into());
        assert!(!brainstorm_questions_overlap(&first, &repeated));
        repeated.parent_option_id = first.parent_option_id.clone();
        repeated.prompt = "如何约定分享时的个人隐私边界？".into();
        assert!(!brainstorm_questions_overlap(&first, &repeated));
    }

    #[test]
    fn brainstorm_rewording_with_the_same_choice_contract_is_duplicate() {
        let mut answered = test_brainstorm_question("multi_choice", true);
        answered.parent_question_id = Some("root".into());
        answered.parent_option_id = Some("chosen".into());
        answered.prompt = "新号应如何运营以最大化爆款产出？".into();
        answered.options[0].label = "全量自动分发".into();
        answered.options[0].description = "平台自动完成选品、脚本、制作与发布，商家只审核。".into();
        let mut repeated = answered.clone();
        repeated.prompt = "新号内容分发应设定何种自动化程度？".into();
        repeated.options[0].id = "renamed-option".into();
        let answered_turn = BrainstormStoredTurn {
            question: answered.clone(),
            answer: test_brainstorm_answer(&["recommended"], ""),
        };
        assert!(brainstorm_question_repeats_answered(
            &answered_turn,
            &repeated
        ));

        let stored = BrainstormStoredState {
            turns: vec![answered_turn],
            current_question: Some(repeated.clone()),
            open_flags: vec!["自动化边界仍待确认".into(), "不同模式影响仍待确认".into()],
            ..serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap()
        };
        let restored = parse_brainstorm_state(&serde_json::to_string(&stored).unwrap()).unwrap();
        assert!(restored.current_question.is_none());
        assert!(restored.open_flags.is_empty());
        assert!(restored.invalidated_question_ids.contains(&repeated.id));

        for (index, option) in repeated.options.iter_mut().enumerate() {
            option.label = format!("另一决定候选{}", index);
            option.description = format!("只处理另一项互补决定的具体边界{}。", index);
        }
        let distinct_turn = BrainstormStoredTurn {
            question: answered,
            answer: test_brainstorm_answer(&["recommended"], ""),
        };
        assert!(!brainstorm_question_repeats_answered(
            &distinct_turn,
            &repeated
        ));
    }

    #[test]
    fn brainstorm_extensions_rotate_across_different_parents_before_deeper_questions() {
        let mut stored: BrainstormStoredState =
            serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap();
        let root = test_brainstorm_question("multi_choice", true);
        stored.turns.push(BrainstormStoredTurn { question: root,
            answer: test_brainstorm_answer(&["recommended", "alternative"], "") });
        answer_pending_brainstorm_branch(&mut stored, "solution-a", &["recommended"], "");
        answer_pending_brainstorm_branch(&mut stored, "solution-b", &["recommended"], "");
        answer_pending_brainstorm_branch(&mut stored, "delivery-a", &["recommended"], "");
        stored.pending_extensions.push(BrainstormExtensionPlan {
            id: "facet-a".into(), parent_question_id: "solution-a".into(),
            parent_option_id: Some("recommended".into()), parent_revision: 0,
            goal: "另一项独立执行决定".into(), ordinal: 1,
        });
        let pending = pending_brainstorm_branches(&stored);
        assert_eq!(pending.iter().map(|branch| branch.question.id.as_str()).collect::<Vec<_>>(),
            vec!["solution-b", "solution-a", "delivery-a"]);
        assert_eq!(pending[0].exploration_stage, "implementation");
        assert_eq!(pending[1].extension.unwrap().id, "facet-a");
        assert_eq!(pending[2].exploration_stage, "validation");
    }

    #[test]
    fn brainstorm_branch_queue_covers_single_choices_and_custom_answers() {
        for (selected, custom) in [(vec!["recommended"], ""), (vec![], "先办一次小型试演")] {
            let mut stored: BrainstormStoredState =
                serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap();
            let root = test_brainstorm_question("single_choice", true);
            stored.turns.push(BrainstormStoredTurn {question: root.clone(),
                answer: test_brainstorm_answer(&selected, custom)});
            let branch = pending_brainstorm_branch(&stored).unwrap();
            assert_eq!(branch.question.id, root.id);
            assert_eq!(branch.option.is_some(), !selected.is_empty());
            answer_pending_brainstorm_branch(&mut stored, "solution", &[], "先做原型");
            assert_eq!(stored.turns[1].question.parent_question_id.as_deref(), Some(root.id.as_str()));
            assert_eq!(stored.turns[1].question.parent_option_id.as_deref(), selected.first().copied());
            answer_pending_brainstorm_branch(&mut stored, "delivery", &[], "下周试演");
            answer_pending_brainstorm_branch(&mut stored, "validation", &[], "收集参与者反馈");
            assert!(pending_brainstorm_branch(&stored).is_none());
        }
    }

    #[test]
    fn brainstorm_branch_queue_preserves_skip_exclude_and_current_manual_intent() {
        let mut stored: BrainstormStoredState =
            serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap();
        let root = test_brainstorm_question("multi_choice", true);
        stored.turns.push(BrainstormStoredTurn {question: root.clone(),
            answer: test_brainstorm_answer(&["recommended"], "")});
        answer_pending_brainstorm_branch(&mut stored, "old-solution", &["recommended"], "");
        stored.brief_edits.insert(root.id.clone(), "新的方向".to_string());
        let branch = pending_brainstorm_branch(&stored).unwrap();
        assert_eq!(branch.question.id, root.id);
        assert!(branch.option.is_none());
        assert_eq!(branch.focus, "新的方向");
        stored.brief_edits.insert(root.id.clone(), String::new());
        assert!(pending_brainstorm_branch(&stored).is_none());
        stored.brief_edits.clear();
        for source in ["agent_assumption", "user_excluded"] {
            stored.turns[0].answer.source = source.to_string();
            // Even a malformed old skipped answer with selected options cannot create choices.
            assert!(pending_brainstorm_branch(&stored).is_none());
        }
    }

    #[test]
    fn brainstorm_branch_focus_preserves_ancestry_without_cached_option_evidence() {
        let mut stored: BrainstormStoredState =
            serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap();
        let mut root = test_brainstorm_question("multi_choice", true);
        root.options[0].label = "自由交流".to_string();
        root.options[0].description = "PRIVATE-CACHED-EVIDENCE".to_string();
        root.options[0].details = "PRIVATE-DETAIL-EVIDENCE".to_string();
        stored.turns.push(BrainstormStoredTurn {question: root,
            answer: test_brainstorm_answer(&["recommended"], "")});
        answer_pending_brainstorm_branch(&mut stored, "solution", &[], "先办小型试演");
        let branch = pending_brainstorm_branch(&stored).unwrap();
        let focus = brainstorm_branch_focus(&stored, &branch);
        assert!(focus.contains("自由交流 → 先办小型试演"));
        assert!(!focus.contains("PRIVATE-CACHED-EVIDENCE"));
        assert!(!focus.contains("PRIVATE-DETAIL-EVIDENCE"));
        let context = brainstorm_decision_context(&stored);
        assert_eq!(context[1]["parent_question_id"], "question-1");
        assert_eq!(context[1]["parent_option_id"], "recommended");
        assert_eq!(context[1]["exploration_stage"], "solutions");
    }

    #[test]
    fn brainstorm_custom_summary_edits_do_not_reuse_a_stale_or_late_answered_subtree() {
        let mut stored: BrainstormStoredState =
            serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap();
        let root = test_brainstorm_question("multi_choice", true);
        stored.turns.push(BrainstormStoredTurn {question: root.clone(),
            answer: test_brainstorm_answer(&[], "原来自定义方向")});
        stored.user_input_revisions.insert(root.id.clone(), 1);
        answer_pending_brainstorm_branch(&mut stored, "old-custom-solution", &["recommended"], "");
        assert_eq!(stored.branch_parent_revisions["old-custom-solution"], 1);
        stored.brief_edits.insert(root.id.clone(), "新的自定义方向".to_string());
        stored.user_input_revisions.insert(root.id.clone(), 2);
        // The old question may be answered after its parent was edited. Its
        // answer revision cannot make its earlier generation context current.
        stored.user_input_revisions.insert("old-custom-solution".to_string(), 3);
        assert_eq!(brainstorm_decision_context(&stored).len(), 1);
        assert!(!brainstorm_brief(&stored).contains("推荐答案"));
        let branch = pending_brainstorm_branch(&stored).unwrap();
        assert_eq!(branch.question.id, root.id);
        assert_eq!(branch.focus, "新的自定义方向");
        answer_pending_brainstorm_branch(&mut stored, "new-custom-solution", &[], "新解法");
        answer_pending_brainstorm_branch(&mut stored, "new-delivery", &[], "新落地步骤");
        answer_pending_brainstorm_branch(&mut stored, "new-validation", &[], "新的验证计划");
        assert!(pending_brainstorm_branch(&stored).is_none());
        stored.brief_edits.insert(root.id.clone(), "再次调整的方向".to_string());
        stored.user_input_revisions.insert(root.id.clone(), 7);
        let restored: BrainstormStoredState = serde_json::from_str(&serde_json::to_string(&stored).unwrap()).unwrap();
        let branch = pending_brainstorm_branch(&restored).unwrap();
        assert_eq!(branch.question.id, root.id);
        assert_eq!(branch.focus, "再次调整的方向");
    }

    #[test]
    fn brainstorm_question_stage_supports_legacy_and_rejects_explicit_stage_mismatch() {
        let mut stored: BrainstormStoredState =
            serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap();
        let question = test_brainstorm_question("multi_choice", true);
        let encoded = serde_json::to_value(&question).unwrap();
        assert!(encoded.get("exploration_stage").is_none());
        let legacy: BrainstormQuestion = serde_json::from_value(encoded).unwrap();
        assert_eq!(brainstorm_exploration_stage(&legacy), "explore");
        stored.turns.push(BrainstormStoredTurn {question: legacy,
            answer: test_brainstorm_answer(&["recommended"], "")});
        let branch = pending_brainstorm_branch(&stored).unwrap();
        let mut generated = question.clone();
        assert!(apply_brainstorm_question_stage(&mut generated, Some(&branch)));
        assert_eq!(generated.exploration_stage.as_deref(), Some("solutions"));
        generated.exploration_stage = Some("explore".to_string());
        assert!(!apply_brainstorm_question_stage(&mut generated, Some(&branch)));
        generated.exploration_stage = Some("invented".to_string());
        assert!(!apply_brainstorm_question_stage(&mut generated, None));
        generated.exploration_stage = Some("implementation".to_string());
        assert!(apply_brainstorm_question_stage(&mut generated, None));
    }

    #[test]
    fn brainstorm_new_continuation_takes_precedence_over_older_roots() {
        let mut stored: BrainstormStoredState =
            serde_json::from_value(serde_json::json!({"root_request":"测试"})).unwrap();
        let root = test_brainstorm_question("multi_choice", true);
        stored.turns.push(BrainstormStoredTurn {question: root.clone(),
            answer: test_brainstorm_answer(&["recommended", "alternative"], "")});
        let mut continuation = root;
        continuation.id = "continuation".to_string();
        continuation.dimension_id = "continuation_directions".to_string();
        stored.turns.push(BrainstormStoredTurn {question: continuation,
            answer: test_brainstorm_answer(&[], "新增方向")});
        let branch = pending_brainstorm_branch(&stored).unwrap();
        assert_eq!(branch.question.id, "continuation");
        assert_eq!(branch.focus, "新增方向");
    }
}
