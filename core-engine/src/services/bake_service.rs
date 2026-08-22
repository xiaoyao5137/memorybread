use std::collections::{HashMap, HashSet};
use std::time::Duration;

use axum::http::StatusCode;
use chrono::{Local, TimeZone, Utc};
use futures::StreamExt;
use serde::{Deserialize, Deserializer, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::api::error::ApiError;
use crate::services::document_refresh::{
    evaluate_document_refresh as evaluate_refresh_decision, is_refreshable_document_url,
    is_valid_document_refresh_policy, source_text_fingerprint, DocumentRefreshDecision,
};
use crate::storage::document_identity::{
    canonical_document_identity, canonical_document_source_title, is_generic_document_source_title,
};
use crate::storage::models::CaptureRecord;
use crate::storage::repo::favorite::{
    FAVORITE_KIND_DOCUMENT, FAVORITE_KIND_KNOWLEDGE, FAVORITE_KIND_OPERATION,
};
use crate::storage::{
    now_ms, BakeActionTraceRecord, BakeActivityRecord, BakeDocumentRecord,
    BakeDocumentSourceSnapshotRecord, BakeKnowledgeRecord, BakeMemorySourceRecord,
    BakeOverviewRecord, BakeRunRecord, BakeSopRecord, DataSourceRecord, NewBakeArtifactAudit,
    NewBakeCandidateAudit, NewBakeDocument, NewBakeDocumentSourceSnapshot, NewBakeKnowledge,
    NewBakeRun, NewBakeSop, NewTimeline, StorageError, StorageManager, TimelineRecord,
};

const BAKE_STYLE_CONFIG_KEY: &str = "bake.style.config";
const CATEGORY_BAKE_ARTICLE: &str = "bake_article";
const CATEGORY_BAKE_SOP: &str = "bake_sop";
const CATEGORY_BAKE_KNOWLEDGE: &str = "bake_knowledge";
const UNIFIED_BAKE_PIPELINE_NAME: &str = "unified";
pub(crate) const BAKE_GENERATION_VERSION: &str = "bake-v1";
// sidecar 对普通输入使用 180 秒、>=20K 长输入使用 300 秒运行时预算。
// Core 多留 10 秒用于接收 504 和连接收尾，不能先断开后留下幽灵推理。
const BAKE_SIDECAR_TIMEOUT_SECS: u64 = 310;
/// 整个 bake run 的最大执行时间（含候选查询、LLM 提炼、数据库写入）。
/// 超过此时间强制标记为 failed，防止因死锁或无限等待导致 run 永久挂起。
const BAKE_RUN_MAX_TOTAL_SECS: u64 = 30 * 60;
/// 单条候选最多执行三次。
///
/// 超时和模型结构化输出截断会先把批次标记为 deferred，由后台调度按退避策略
/// 重新触发；达到此上限后才进入终态，避免一次偶发慢请求直接丢失候选。
pub(crate) const MAX_BAKE_RETRY_FAILURES: i64 = 3;
const BAKE_SOP_ZERO_OUTPUT_ELIGIBLE_ALERT_THRESHOLD: i64 = 20;
const KNOWLEDGE_PUBLISH_SCORE: f64 = 0.78;
const KNOWLEDGE_SHADOW_SCORE: f64 = 0.62;
const KNOWLEDGE_DECISION_RULE_VERSION: &str = "knowledge-open-semantic-v1";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakePagedResponse<T> {
    pub items: Vec<T>,
    pub total: i64,
    pub limit: usize,
    pub offset: usize,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BakeBucket {
    Extracted,
    Pending,
}

impl BakeBucket {
    pub fn from_query(value: Option<&str>) -> Result<Option<Self>, ApiError> {
        match value.map(str::trim).filter(|value| !value.is_empty()) {
            None => Ok(None),
            Some("extracted") => Ok(Some(Self::Extracted)),
            Some("pending") => Ok(Some(Self::Pending)),
            Some(other) => Err(ApiError::BadRequest(format!(
                "invalid bake bucket: {other}"
            ))),
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct BakeMemoryFilter {
    pub q: Option<String>,
    pub from_ts: Option<i64>,
    pub to_ts: Option<i64>,
    pub limit: usize,
    pub offset: usize,
}

#[derive(Debug, Clone, Default)]
pub struct BakeListFilter {
    pub q: Option<String>,
    pub bucket: Option<BakeBucket>,
    pub from_ts: Option<i64>,
    pub to_ts: Option<i64>,
    pub favorite: Option<bool>,
    pub limit: usize,
    pub offset: usize,
    pub sort: BakeListSort,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum BakeListSort {
    #[default]
    Recent,
    Heat,
}

#[derive(Debug, Clone, Default)]
pub struct BakeCaptureFilter {
    pub q: Option<String>,
    pub app_name: Option<String>,
    pub from_ts: Option<i64>,
    pub to_ts: Option<i64>,
    pub source_capture_id: Option<i64>,
    pub limit: usize,
    pub offset: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeCapturePayload {
    pub id: String,
    pub ts: i64,
    pub app_name: Option<String>,
    pub app_bundle_id: Option<String>,
    pub win_title: Option<String>,
    pub event_type: String,
    pub semantic_type_label: String,
    pub raw_type_label: String,
    pub ax_text: Option<String>,
    pub ax_focused_role: Option<String>,
    pub ax_focused_id: Option<String>,
    pub ocr_text: Option<String>,
    pub input_text: Option<String>,
    pub audio_text: Option<String>,
    pub screenshot_path: Option<String>,
    pub screenshot_source: Option<String>,
    pub url: Option<String>,
    pub webpage_title: Option<String>,
    pub is_sensitive: bool,
    pub pii_scrubbed: bool,
    pub best_text: Option<String>,
    pub summary: Option<String>,
    pub linked_timeline_id: Option<String>,
    pub linked_timeline_summary: Option<String>,
}

/// 时间线详情回溯区用的关联产物集合：一次定向查询拿到知识/文档/操作/数据。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TimelineRelationsPayload {
    pub timeline_id: i64,
    pub knowledge: Option<BakeKnowledgePayload>,
    pub document: Option<BakeDocumentPayload>,
    pub sop: Option<BakeSopPayload>,
    pub data: Option<DataSourceRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeKnowledgePayload {
    pub id: String,
    pub is_favorite: bool,
    pub capture_id: String,
    pub source_capture_ids: Vec<String>,
    pub source_timeline_id: String,
    pub source_url: Option<String>,
    pub summary: String,
    pub overview: Option<String>,
    pub details: Option<String>,
    pub detailed_content: Option<String>,
    pub entities: Vec<String>,
    pub category: String,
    pub importance: i64,
    pub occurrence_count: i64,
    pub observed_at: Option<i64>,
    pub status: String,
    pub review_status: String,
    pub match_score: Option<f64>,
    pub match_level: Option<String>,
    pub created_at: String,
    pub created_at_ms: i64,
    pub updated_at: String,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeStyleConfig {
    pub preferred_phrases: Vec<String>,
    pub replacement_rules: Vec<ReplacementRulePayload>,
    pub style_samples: Vec<String>,
    pub apply_to_creation: bool,
    pub apply_to_template_editing: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReplacementRulePayload {
    pub from: String,
    pub to: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DocumentSectionPayload {
    pub title: String,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub keywords: Vec<String>,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub notes: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeDocumentPayload {
    pub id: String,
    pub is_favorite: bool,
    pub title: String,
    pub doc_type: String,
    pub status: String,
    pub tags: Vec<String>,
    pub applicable_tasks: Vec<String>,
    pub source_memory_ids: Vec<String>,
    pub source_capture_ids: Vec<String>,
    pub source_episode_ids: Vec<String>,
    pub linked_knowledge_ids: Vec<String>,
    pub sections: Vec<DocumentSectionPayload>,
    pub style_phrases: Vec<String>,
    pub replacement_rules: Vec<ReplacementRulePayload>,
    pub summary: Option<String>,
    pub full_content: Option<String>,
    pub prompt_hint: Option<String>,
    pub diagram_code: Option<String>,
    pub image_assets: Vec<String>,
    pub source_url: Option<String>,
    pub usage_count: i64,
    pub match_score: Option<f64>,
    pub match_level: Option<String>,
    pub creation_mode: String,
    pub review_status: String,
    pub evidence_summary: Option<String>,
    pub generation_version: Option<String>,
    pub refresh_policy: String,
    pub last_refresh_checked_at_ms: i64,
    pub last_refresh_error: Option<String>,
    pub last_refresh_success_at_ms: i64,
    pub last_refresh_status: String,
    pub last_refresh_completeness: String,
    pub last_refresh_content_hash: Option<String>,
    pub last_refresh_character_count: i64,
    pub last_refresh_segment_count: i64,
    pub last_refresh_truncated: bool,
    pub deleted_at: Option<i64>,
    pub created_at: String,
    pub created_at_ms: i64,
    pub updated_at: String,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeMemoryPayload {
    pub id: String,
    pub title: String,
    pub url: Option<String>,
    pub source_capture_id: Option<String>,
    pub source_timeline_id: Option<String>,
    pub details: Option<String>,
    pub summary: Option<String>,
    pub weight: i64,
    pub open_count: i64,
    pub dwell_seconds: i64,
    pub has_edit_action: bool,
    pub knowledge_ref_count: i64,
    pub status: String,
    pub suggested_action: Option<String>,
    pub tags: Vec<String>,
    pub last_visited_at: Option<String>,
    pub created_at: String,
    pub created_at_ms: i64,
    pub knowledge_match_score: Option<f64>,
    pub knowledge_match_level: Option<String>,
    pub template_match_score: Option<f64>,
    pub template_match_level: Option<String>,
    pub sop_match_score: Option<f64>,
    pub sop_match_level: Option<String>,
    pub capture_ids: Vec<i64>,
    #[serde(rename = "keyTimestamps")]
    pub key_timestamps: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeLinkedKnowledgeSummaryPayload {
    pub id: String,
    pub summary: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeSopPayload {
    pub id: String,
    pub is_favorite: bool,
    pub source_capture_id: String,
    pub source_timeline_id: String,
    pub source_title: Option<String>,
    pub trigger_keywords: Vec<String>,
    pub confidence: String,
    pub extracted_problem: Option<String>,
    pub detailed_content: Option<String>,
    pub steps: Vec<String>,
    pub linked_knowledge_ids: Vec<String>,
    pub linked_knowledge_summaries: Vec<BakeLinkedKnowledgeSummaryPayload>,
    pub status: String,
    pub created_at: String,
    pub created_at_ms: i64,
    pub updated_at: String,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeOverviewPayload {
    pub capture_count: i64,
    pub memory_count: i64,
    pub data_count: i64,
    pub knowledge_count: i64,
    pub template_count: i64,
    pub sop_count: i64,
    pub pending_candidates: i64,
    pub auto_created_today: i64,
    pub candidate_today: i64,
    pub discarded_today: i64,
    pub last_bake_run_status: Option<String>,
    pub last_bake_run_at: Option<i64>,
    pub last_trigger_reason: Option<String>,
    pub knowledge_auto_count: i64,
    pub template_auto_count: i64,
    pub sop_auto_count: i64,
    pub recent_activities: Vec<String>,
    pub inventory_trend: Vec<BakeInventoryTrendBucketPayload>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeInventoryTrendBucketPayload {
    pub label: String,
    pub start_ts: i64,
    pub end_ts: i64,
    pub memory_count: i64,
    pub data_count: i64,
    pub knowledge_count: i64,
    pub template_count: i64,
    pub sop_count: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeRunPayload {
    pub id: String,
    pub trigger_reason: String,
    pub status: String,
    pub started_at: i64,
    pub completed_at: Option<i64>,
    pub processed_episode_count: i64,
    pub auto_created_count: i64,
    pub candidate_count: i64,
    pub discarded_count: i64,
    pub knowledge_created_count: i64,
    pub document_created_count: i64,
    pub sop_created_count: i64,
    pub error_message: Option<String>,
    pub latency_ms: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct InitializeBakeMemoriesResponse {
    pub created_count: i64,
    pub skipped_count: i64,
    pub articles: Vec<BakeMemoryPayload>,
    pub memories: Vec<BakeMemoryPayload>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeExtractRequest {
    pub trigger_reason: String,
    /// 已记录的失败次数。sidecar 可据此在重试时启用更紧凑的结构化输出策略。
    pub retry_attempt: i64,
    /// 上一次失败的稳定错误码，用于选择针对性的重试预算和提示词。
    #[serde(default)]
    pub retry_error_code: Option<String>,
    pub candidate: BakeExtractCandidatePayload,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeExtractCandidatePayload {
    pub source_timeline_id: i64,
    pub source_capture_id: i64,
    /// 当前时间线实际关联的采集记录数。操作提炼至少需要两帧；旧请求缺失时按 0
    /// 处理，由 sidecar 保守降级为单帧。
    #[serde(default)]
    pub source_capture_count: i64,
    /// 严格按时间排序后，真正包含用户交互、导航或可观察结果变化的操作证据
    /// 节点数；不再等同于 action_trace 的原始帧数。
    #[serde(default)]
    pub effective_capture_count: i64,
    /// Core 选择的证据通道。Sidecar 只在同一次 bundle 推理中消费它，
    /// 不会再启动一次 SOP 专用推理。
    #[serde(default)]
    pub sop_evidence_mode: Option<String>,
    pub timeline_category: String,
    pub summary: String,
    pub overview: Option<String>,
    pub details: Option<String>,
    #[serde(default)]
    pub work_item: Option<String>,
    #[serde(default)]
    pub work_status: Option<String>,
    #[serde(default)]
    pub work_progress: Option<String>,
    pub entities: Vec<String>,
    pub importance: i64,
    pub occurrence_count: Option<i64>,
    pub observed_at: Option<i64>,
    pub event_time_start: Option<i64>,
    pub event_time_end: Option<i64>,
    pub start_time: Option<i64>,
    pub end_time: Option<i64>,
    pub duration_minutes: Option<i64>,
    pub time_range_start: Option<i64>,
    pub time_range_end: Option<i64>,
    pub key_timestamps: Option<Value>,
    pub history_view: bool,
    pub content_origin: Option<String>,
    pub activity_type: Option<String>,
    pub evidence_strength: Option<String>,
    pub capture_ts: i64,
    pub capture_app_name: Option<String>,
    pub capture_win_title: Option<String>,
    pub capture_ax_text: Option<String>,
    pub capture_ocr_text: Option<String>,
    pub capture_input_text: Option<String>,
    pub capture_audio_text: Option<String>,
    pub capture_url: Option<String>,
    pub capture_webpage_title: Option<String>,
    pub url_aggregated_text: Option<String>,
    pub url_aggregated_capture_count: i64,
    #[serde(default)]
    pub action_trace: Vec<BakeActionTraceRecord>,
    pub document_evidence: BakeDocumentEvidencePayload,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BakeDocumentEvidenceKind {
    Insufficient,
    DocumentUrl,
    BrowserDocument,
    NativeDocument,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BakeSourceSurface {
    Chat,
    Browser,
    DocumentEditor,
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BakeDocumentEvidencePayload {
    pub kind: BakeDocumentEvidenceKind,
    pub source_surface: BakeSourceSurface,
    pub has_document_url: bool,
    pub has_document_page_title: bool,
    pub has_substantive_document_body: bool,
    pub allows_auto_create: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeExtractResponse {
    pub knowledge: BakeArtifactExtraction,
    #[serde(rename = "design", alias = "document")]
    pub document: BakeArtifactExtraction,
    pub sop: BakeArtifactExtraction,
    #[serde(default)]
    pub primary_type: Option<String>,
    #[serde(default)]
    pub classification_reason: Option<String>,
    pub usage: Option<Value>,
    pub model: Option<String>,
    pub degraded: Option<bool>,
    #[serde(default)]
    pub artifact_shapes: Option<Value>,
    #[serde(default)]
    pub compatibility_recovered: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeArtifactExtraction {
    pub accepted: bool,
    pub reason: Option<String>,
    pub payload: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeMergeDocumentRequest {
    pub existing_document: Value,
    pub candidate: BakeExtractCandidatePayload,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeMergeDocumentResponse {
    pub title: Option<String>,
    pub summary: Option<String>,
    pub full_content: Option<String>,
    pub evidence_summary: Option<String>,
    pub match_score: Option<f64>,
    pub match_level: Option<String>,
    #[serde(default)]
    pub no_change: bool,
}

/// 浏览器刷新回写用的最小 candidate：不构造完整 timeline/capture 记录，
/// 避免把刷新抓取伪装成真实 capture 污染来源关联；sidecar 合并只依赖
/// source_timeline_id 与 url_aggregated_text。
#[derive(Debug, Clone, Serialize)]
struct DocumentRefreshMergeCandidate {
    source_timeline_id: i64,
    summary: String,
    capture_url: String,
    capture_app_name: String,
    url_aggregated_text: String,
    document_refresh_scrape: bool,
}

#[derive(Debug, Clone, Serialize)]
struct DocumentRefreshMergeRequest {
    existing_document: Value,
    candidate: DocumentRefreshMergeCandidate,
}

/// 刷新合并的对外结果：no_change 表示页面内容没有变化或已见过，
/// updated 表示有新内容已合入文档。
#[derive(Debug, Clone, Serialize)]
pub struct DocumentRefreshOutcome {
    pub status: String,
    pub reason: Option<String>,
    pub document: BakeDocumentPayload,
}

#[derive(Debug, Clone, Serialize)]
pub struct DocumentSourceSnapshotPayload {
    pub id: i64,
    pub document_id: i64,
    pub source_url: String,
    pub page_title: String,
    pub content_text: String,
    pub content_hash: String,
    pub completeness_status: String,
    pub identity_match: bool,
    pub reached_end: bool,
    pub stable_passes: i64,
    pub segment_count: i64,
    pub character_count: i64,
    pub truncated: bool,
    pub collector: String,
    pub collected_at: i64,
}

impl From<BakeDocumentSourceSnapshotRecord> for DocumentSourceSnapshotPayload {
    fn from(record: BakeDocumentSourceSnapshotRecord) -> Self {
        Self {
            id: record.id,
            document_id: record.document_id,
            source_url: record.source_url,
            page_title: record.page_title,
            content_text: record.content_text,
            content_hash: record.content_hash,
            completeness_status: record.completeness_status,
            identity_match: record.identity_match,
            reached_end: record.reached_end,
            stable_passes: record.stable_passes,
            segment_count: record.segment_count,
            character_count: record.character_count,
            truncated: record.truncated,
            collector: record.collector,
            collected_at: record.collected_at,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeKnowledgeArtifactPayload {
    #[serde(default)]
    pub summary: String,
    pub overview: Option<String>,
    pub details: Option<String>,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub entities: Vec<String>,
    pub importance: Option<i64>,
    pub occurrence_count: Option<i64>,
    pub observed_at: Option<i64>,
    pub event_time_start: Option<i64>,
    pub event_time_end: Option<i64>,
    pub history_view: Option<bool>,
    pub content_origin: Option<String>,
    pub activity_type: Option<String>,
    pub evidence_strength: Option<String>,
    pub evidence_summary: Option<String>,
    pub future_question: Option<String>,
    pub decision_reason: Option<String>,
    pub match_score: Option<f64>,
    pub match_level: Option<String>,
    pub review_status: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeDocumentArtifactPayload {
    #[serde(default, rename = "name", alias = "title")]
    pub title: String,
    #[serde(rename = "category", alias = "doc_type")]
    pub doc_type: Option<String>,
    pub summary: Option<String>,
    pub full_content: Option<String>,
    pub details: Option<String>,
    pub prompt_hint: Option<String>,
    pub status: Option<String>,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub tags: Vec<String>,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub applicable_tasks: Vec<String>,
    #[serde(
        default,
        rename = "structure_sections",
        alias = "sections",
        deserialize_with = "deserialize_document_sections_mixed"
    )]
    pub sections: Vec<DocumentSectionPayload>,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub style_phrases: Vec<String>,
    #[serde(default)]
    pub replacement_rules: Vec<ReplacementRulePayload>,
    pub diagram_code: Option<String>,
    pub evidence_summary: Option<String>,
    pub match_score: Option<f64>,
    pub match_level: Option<String>,
    pub review_status: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeSopArtifactPayload {
    #[serde(default)]
    pub summary: String,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub overview: Option<String>,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub details: Option<String>,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub source_title: Option<String>,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub trigger_keywords: Vec<String>,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub extracted_problem: Option<String>,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub steps: Vec<String>,
    #[serde(default)]
    pub step_evidence: Vec<BakeSopStepEvidencePayload>,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub linked_knowledge_ids: Vec<String>,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub confidence: Option<String>,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub evidence_summary: Option<String>,
    pub match_score: Option<f64>,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub match_level: Option<String>,
    #[serde(default, deserialize_with = "deserialize_optional_string_mixed")]
    pub review_status: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeSopStepEvidencePayload {
    pub step_index: i64,
    #[serde(default, deserialize_with = "deserialize_string_vec_mixed")]
    pub capture_ids: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct BakeSidecarError {
    pub status: StatusCode,
    pub code: &'static str,
    pub message: String,
}

#[derive(Clone)]
pub struct BakeService {
    storage: StorageManager,
    sidecar_url: String,
    client: reqwest::Client,
}

impl BakeService {
    pub fn new(storage: StorageManager, sidecar_url: impl Into<String>) -> Self {
        Self {
            storage,
            sidecar_url: sidecar_url.into(),
            client: reqwest::Client::new(),
        }
    }

    pub async fn preview_memory(
        &self,
        id: i64,
        trigger_reason: &str,
    ) -> Result<BakeExtractResponse, ApiError> {
        let memory = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("memory {id} not found")))?;
        if memory.category != CATEGORY_BAKE_ARTICLE {
            return Err(ApiError::BadRequest(format!(
                "knowledge {id} is not in category {CATEGORY_BAKE_ARTICLE}"
            )));
        }

        let details = parse_details(memory.details.as_deref());
        let source_timeline_id = details
            .get("source_timeline_id")
            .or_else(|| details.get("source_knowledge_id"))
            .and_then(Value::as_i64)
            .ok_or_else(|| {
                ApiError::BadRequest(format!("memory {id} missing source_timeline_id"))
            })?;

        let source_timeline = self
            .storage
            .get_timeline_entry(source_timeline_id)?
            .ok_or_else(|| {
                ApiError::NotFound(format!("source knowledge {source_timeline_id} not found"))
            })?;

        let capture = self
            .storage
            .get_capture(source_timeline.capture_id)?
            .ok_or_else(|| {
                ApiError::NotFound(format!("capture {} not found", source_timeline.capture_id))
            })?;

        let candidate = BakeMemorySourceRecord {
            timeline: source_timeline,
            capture_ts: capture.ts,
            capture_app_name: capture.app_name,
            capture_win_title: capture.win_title,
            capture_ax_text: capture.ax_text,
            capture_ocr_text: capture.ocr_text,
            capture_input_text: capture.input_text,
            capture_audio_text: capture.audio_text,
            capture_url: None,
            capture_webpage_title: None,
            preferred_source_title: None,
            url_aggregated_text: None,
            url_aggregated_capture_count: 0,
            action_trace: Vec::new(),
            work_item: None,
            work_status: None,
            work_progress: None,
            retry_failure_count: 0,
            retry_error_code: None,
            retry_next_at_ms: 0,
        };

        self.extract_candidate(trigger_reason, &candidate).await
    }

    pub fn get_style_config(&self) -> Result<BakeStyleConfig, ApiError> {
        let maybe_value = self.storage.get_preference_value(BAKE_STYLE_CONFIG_KEY)?;
        if let Some(value) = maybe_value {
            serde_json::from_str::<BakeStyleConfig>(&value)
                .map_err(|err| ApiError::Internal(format!("解析 bake.style.config 失败: {err}")))
        } else {
            Ok(default_style_config())
        }
    }

    pub fn save_style_config(&self, config: &BakeStyleConfig) -> Result<BakeStyleConfig, ApiError> {
        let value = serde_json::to_string(config)
            .map_err(|err| ApiError::Internal(format!("序列化写作自然感配置失败: {err}")))?;
        self.storage
            .upsert_preference(BAKE_STYLE_CONFIG_KEY, &value, "user", 1.0)?;
        Ok(config.clone())
    }

    pub fn list_documents(&self) -> Result<Vec<BakeDocumentPayload>, ApiError> {
        let favorite_ids = self
            .storage
            .list_memory_favorite_ids(FAVORITE_KIND_DOCUMENT)?;
        Ok(self
            .storage
            .list_bake_documents()?
            .into_iter()
            .filter(is_current_bake_document)
            .map(|record| {
                let is_favorite = favorite_ids.contains(&record.id);
                map_document_record(record, is_favorite)
            })
            .collect())
    }

    pub fn list_documents_paginated(
        &self,
        filter: BakeListFilter,
    ) -> Result<BakePagedResponse<BakeDocumentPayload>, ApiError> {
        self.list_documents_paginated_with_type(filter, None)
    }

    pub fn list_documents_paginated_with_type(
        &self,
        filter: BakeListFilter,
        doc_type: Option<&str>,
    ) -> Result<BakePagedResponse<BakeDocumentPayload>, ApiError> {
        let exact_id = filter.q.as_deref().and_then(parse_exact_list_id);
        let favorite_ids = self
            .storage
            .list_memory_favorite_ids(FAVORITE_KIND_DOCUMENT)?;
        let mut items = self
            .storage
            .list_bake_documents()?
            .into_iter()
            .filter(is_current_bake_document)
            .filter(|record| exact_id.map_or(true, |id| record.id == id))
            .filter(|record| matches_document_bucket(record, filter.bucket))
            .filter(|record| {
                filter.favorite.map_or(true, |favorite| {
                    favorite_ids.contains(&record.id) == favorite
                })
            })
            .filter(|record| {
                filter
                    .from_ts
                    .map_or(true, |from| record.created_at >= from)
            })
            .filter(|record| filter.to_ts.map_or(true, |to| record.created_at <= to))
            .map(|record| {
                let is_favorite = favorite_ids.contains(&record.id);
                map_document_record(record, is_favorite)
            })
            .collect::<Vec<_>>();

        if let Some(doc_type) = doc_type.map(str::trim).filter(|value| !value.is_empty()) {
            let doc_type_lower = doc_type.to_lowercase();
            items.retain(|item| {
                item.doc_type
                    .split(|character| {
                        matches!(character, '、' | ',' | '，' | '/' | '|' | ';' | '；')
                    })
                    .any(|category| category.trim().to_lowercase() == doc_type_lower)
            });
        }

        if exact_id.is_none() {
            if let Some(query) = filter.q.as_deref() {
                let query_lower = query.to_lowercase();
                items.retain(|item| {
                    item.title.to_lowercase().contains(&query_lower)
                        || item.doc_type.to_lowercase().contains(&query_lower)
                        || item
                            .prompt_hint
                            .as_deref()
                            .unwrap_or_default()
                            .to_lowercase()
                            .contains(&query_lower)
                        || item
                            .summary
                            .as_deref()
                            .unwrap_or_default()
                            .to_lowercase()
                            .contains(&query_lower)
                        || item
                            .full_content
                            .as_deref()
                            .unwrap_or_default()
                            .to_lowercase()
                            .contains(&query_lower)
                        || item
                            .source_url
                            .as_deref()
                            .unwrap_or_default()
                            .to_lowercase()
                            .contains(&query_lower)
                        || item
                            .tags
                            .iter()
                            .any(|tag| tag.to_lowercase().contains(&query_lower))
                        || item.sections.iter().any(|section| {
                            section.title.to_lowercase().contains(&query_lower)
                                || section
                                    .notes
                                    .as_deref()
                                    .unwrap_or_default()
                                    .to_lowercase()
                                    .contains(&query_lower)
                                || section
                                    .keywords
                                    .iter()
                                    .any(|keyword| keyword.to_lowercase().contains(&query_lower))
                        })
                });
            }
        }

        let total = items.len() as i64;
        let items = items
            .into_iter()
            .skip(filter.offset)
            .take(filter.limit)
            .collect();
        Ok(BakePagedResponse {
            items,
            total,
            limit: filter.limit,
            offset: filter.offset,
        })
    }

    pub fn create_document(
        &self,
        payload: CreateOrUpdateDocumentRequest,
    ) -> Result<BakeDocumentPayload, ApiError> {
        let record = request_to_new_document(payload)?;
        let id = self.storage.insert_bake_document(&record)?;
        let created = self
            .storage
            .get_bake_document(id)?
            .ok_or_else(|| ApiError::NotFound(format!("document {id} not found after insert")))?;
        Ok(map_document_record(created, false))
    }

    pub fn get_document(&self, id: i64) -> Result<BakeDocumentPayload, ApiError> {
        let record = self
            .storage
            .get_bake_document(id)?
            .filter(is_current_bake_document)
            .ok_or_else(|| ApiError::NotFound(format!("document {id} not found")))?;
        let is_favorite = self
            .storage
            .is_memory_favorite(FAVORITE_KIND_DOCUMENT, record.id)?;
        Ok(map_document_record(record, is_favorite))
    }

    /// 刷新资格评估：返回文档记录与判定结论，供 handler 在浏览器采集前
    /// 做一次性门禁检查，避免对不可刷新文档白开一次浏览器。
    pub fn evaluate_document_refresh(
        &self,
        id: i64,
        now_ms: i64,
    ) -> Result<(BakeDocumentRecord, DocumentRefreshDecision), ApiError> {
        let record = self
            .storage
            .get_bake_document(id)?
            .filter(is_current_bake_document)
            .ok_or_else(|| ApiError::NotFound(format!("document {id} not found")))?;
        let fingerprints = self.storage.list_bake_document_source_fingerprints(id)?;
        let url_valid = record
            .source_url
            .as_deref()
            .map(is_refreshable_document_url)
            .unwrap_or(false);
        let decision = evaluate_refresh_decision(&record, &fingerprints, now_ms, url_valid);
        Ok((record, decision))
    }

    /// 刷新策略可被用户覆盖：auto/always/never。
    pub fn set_document_refresh_policy(
        &self,
        id: i64,
        policy: &str,
    ) -> Result<BakeDocumentPayload, ApiError> {
        if !is_valid_document_refresh_policy(policy) {
            return Err(ApiError::BadRequest(format!(
                "invalid refresh policy: {policy}"
            )));
        }
        if !self.storage.set_bake_document_refresh_policy(id, policy)? {
            return Err(ApiError::NotFound(format!("document {id} not found")));
        }
        self.get_document(id)
    }

    /// 记录刷新失败并推进节流时钟：PAGE_GONE 永久阻止后续刷新，
    /// 其他错误码仅记录，下一个检查窗口仍可重试。
    pub fn record_document_refresh_failure(
        &self,
        id: i64,
        error_code: &str,
        now_ms: i64,
    ) -> Result<(), ApiError> {
        let status = self
            .storage
            .get_bake_document(id)?
            .map(|record| {
                if record.last_refresh_success_at_ms > 0 {
                    "historical_only"
                } else {
                    "unavailable"
                }
            })
            .unwrap_or("unavailable");
        self.storage
            .record_document_refresh_failure(id, now_ms, status, error_code)?;
        Ok(())
    }

    /// 读取最近一次成功保存的来源快照，供节流窗口内的后续创作复用。
    pub fn get_latest_document_source_snapshot(
        &self,
        document_id: i64,
    ) -> Result<Option<DocumentSourceSnapshotPayload>, ApiError> {
        Ok(self
            .storage
            .get_latest_bake_document_source_snapshot(document_id)?
            .map(Into::into))
    }

    /// 保存本轮已校验的原始来源快照，但不直接覆盖烘焙文档。
    /// 返回值的 bool 表示来源内容指纹是否首次观察到。
    pub fn record_document_refresh_snapshot(
        &self,
        snapshot: NewBakeDocumentSourceSnapshot,
    ) -> Result<(bool, DocumentSourceSnapshotPayload), ApiError> {
        let was_seen = self
            .storage
            .has_bake_document_source_fingerprint(snapshot.document_id, &snapshot.content_hash)?;
        let document_id = snapshot.document_id;
        let collected_at = snapshot.collected_at;
        let source_timeline_id = self
            .storage
            .get_bake_document(document_id)?
            .and_then(|record| {
                parse_json_vec_string(&record.source_memory_ids)
                    .first()
                    .and_then(|value| value.parse::<i64>().ok())
            })
            .unwrap_or(document_id);
        let record = self
            .storage
            .upsert_bake_document_source_snapshot(&snapshot)?;
        self.storage.record_bake_document_source_fingerprint(
            document_id,
            &record.content_hash,
            source_timeline_id,
        )?;
        let status = match record.completeness_status.as_str() {
            "complete" => "fresh_complete",
            "partial" => "fresh_partial",
            _ => "unavailable",
        };
        self.storage
            .record_document_refresh_success(document_id, collected_at, status, &record)?;
        let mut payload: DocumentSourceSnapshotPayload = record.into();
        // 相同指纹只持久化一份不可变内容，但本次重新校验的时间仍应返回给
        // Writer，避免把“刚刚确认未变化”误标成旧采集时间。
        payload.collected_at = collected_at;
        Ok((!was_seen, payload))
    }

    /// 把浏览器刷新抓到的正文当作一次“模拟回访”合入已有文档：
    /// 复用与用户回访完全相同的指纹判重与 sidecar 补丁合并，
    /// 保证刷新回写语义与 bake 流水线一致。
    pub async fn merge_document_refresh_scrape(
        &self,
        existing_doc: &BakeDocumentRecord,
        scraped_title: &str,
        scraped_text: &str,
        scraped_url: &str,
        now_ms: i64,
    ) -> Result<DocumentRefreshOutcome, ApiError> {
        let fingerprint = source_text_fingerprint(scraped_text)
            .ok_or_else(|| ApiError::BadRequest("刷新抓取内容为空".to_string()))?;
        if self
            .storage
            .has_bake_document_source_fingerprint(existing_doc.id, &fingerprint)?
        {
            // 内容已见过：推进检查时钟并清除历史错误，不进入 LLM 合并。
            self.storage
                .touch_document_refresh_state(existing_doc.id, now_ms, None)?;
            let document = self.get_document(existing_doc.id)?;
            return Ok(DocumentRefreshOutcome {
                status: "no_change".to_string(),
                reason: Some("source_fingerprint_already_seen".to_string()),
                document,
            });
        }

        let existing_json = serde_json::to_value(existing_doc)
            .map_err(|e| ApiError::Internal(format!("序列化已有文档失败: {e}")))?;
        // source_timeline_id 只用于满足 sidecar 契约与日志标识，
        // 不参与来源关联写入（刷新不产生新的 source_memory_ids）。
        let source_timeline_id = parse_json_vec_string(&existing_doc.source_memory_ids)
            .first()
            .and_then(|value| value.parse::<i64>().ok())
            .unwrap_or(existing_doc.id);
        let request_body = DocumentRefreshMergeRequest {
            existing_document: existing_json,
            candidate: DocumentRefreshMergeCandidate {
                source_timeline_id,
                summary: scraped_title.to_string(),
                capture_url: scraped_url.to_string(),
                capture_app_name: existing_doc
                    .source_app_name
                    .clone()
                    .unwrap_or_else(|| "browser_refresh".to_string()),
                url_aggregated_text: scraped_text.to_string(),
                document_refresh_scrape: true,
            },
        };
        let url = format!("{}/bake/merge_document", self.sidecar_url);
        let response = self
            .client
            .post(&url)
            .json(&request_body)
            .timeout(Duration::from_secs(BAKE_SIDECAR_TIMEOUT_SECS))
            .send()
            .await
            .map_err(map_sidecar_request_error)?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            let status = StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
            let error = map_sidecar_error(status, body, "bake 文档刷新合并服务");
            self.storage.touch_document_refresh_state(
                existing_doc.id,
                now_ms,
                Some("SIDECAR_MERGE_FAILED"),
            )?;
            tracing::warn!(
                "document refresh merge sidecar error doc_id={} status={} code={}",
                existing_doc.id,
                status,
                error.code
            );
            return Err(ApiError::Upstream {
                status: error.status,
                code: error.code,
                message: error.message,
            });
        }

        let merged: BakeMergeDocumentResponse = response.json().await.map_err(|error| {
            tracing::warn!("解析文档刷新合并响应失败: {}", error);
            ApiError::Upstream {
                status: StatusCode::BAD_GATEWAY,
                code: "BAKE_SIDECAR_RESPONSE_INVALID",
                message: "bake 文档刷新合并服务返回了无法解析的响应".to_string(),
            }
        })?;

        let mut update = bake_document_record_to_new(existing_doc.clone());
        let content_updated = if !merged.no_change {
            if let Some(merged_summary) = merged.summary {
                update.summary = Some(merged_summary);
            }
            let mut content_changed = false;
            if let Some(merged_content) = merged.full_content {
                if document_merge_preserves_existing(
                    existing_doc.full_content.as_deref(),
                    &merged_content,
                ) {
                    update.full_content = Some(merged_content);
                    content_changed = true;
                } else {
                    tracing::warn!(
                        "document refresh merge rejected because it would drop existing content: doc_id={} existing_len={} merged_len={}",
                        existing_doc.id,
                        existing_doc.full_content.as_deref().unwrap_or_default().chars().count(),
                        merged_content.chars().count(),
                    );
                }
            }
            if let Some(evidence_summary) = merged.evidence_summary {
                update.evidence_summary = Some(evidence_summary);
            }
            if let Some(match_score) = merged.match_score {
                update.match_score = Some(match_score);
            }
            if let Some(match_level) = merged.match_level {
                update.match_level = Some(match_level);
            }
            content_changed
        } else {
            tracing::info!(
                "document refresh no_change: doc_id={} reason=content_already_covered",
                existing_doc.id,
            );
            false
        };
        // 合并只更新正文，不允许模型把稳定标题改成过程性名称。
        if generated_title_uses_incremental_wording(&update.title) && !scraped_title.is_empty() {
            update.title = scraped_title.to_string();
        }
        if update.full_content.is_some() {
            update.content_hash = update.full_content.as_ref().map(|content| {
                let mut hasher = Sha256::new();
                hasher.update(content.as_bytes());
                format!("{:x}", hasher.finalize())
            });
        }

        self.storage
            .update_bake_document(existing_doc.id, &update)?;
        self.storage.record_bake_document_source_fingerprint(
            existing_doc.id,
            &fingerprint,
            source_timeline_id,
        )?;
        self.storage
            .touch_document_refresh_state(existing_doc.id, now_ms, None)?;

        let document = self.get_document(existing_doc.id)?;
        Ok(DocumentRefreshOutcome {
            status: if content_updated {
                "updated"
            } else {
                "no_change"
            }
            .to_string(),
            reason: None,
            document,
        })
    }

    pub fn adopt_document(&self, id: i64) -> Result<BakeDocumentPayload, ApiError> {
        let mut record = self
            .storage
            .get_bake_document(id)?
            .ok_or_else(|| ApiError::NotFound(format!("document {id} not found")))?;
        record.review_status = "confirmed".to_string();
        if record.status == "draft" {
            record.status = "enabled".to_string();
        }
        let update = bake_document_record_to_new(record);
        self.storage.update_bake_document(id, &update)?;
        let updated = self
            .storage
            .get_bake_document(id)?
            .ok_or_else(|| ApiError::NotFound(format!("document {id} not found after update")))?;
        let is_favorite = self
            .storage
            .is_memory_favorite(FAVORITE_KIND_DOCUMENT, updated.id)?;
        Ok(map_document_record(updated, is_favorite))
    }

    pub fn update_document(
        &self,
        id: i64,
        payload: CreateOrUpdateDocumentRequest,
    ) -> Result<BakeDocumentPayload, ApiError> {
        let record = request_to_new_document(payload)?;
        if !self.storage.update_bake_document(id, &record)? {
            return Err(ApiError::NotFound(format!("document {id} not found")));
        }
        let updated = self
            .storage
            .get_bake_document(id)?
            .ok_or_else(|| ApiError::NotFound(format!("document {id} not found after update")))?;
        let is_favorite = self
            .storage
            .is_memory_favorite(FAVORITE_KIND_DOCUMENT, updated.id)?;
        Ok(map_document_record(updated, is_favorite))
    }

    pub fn toggle_document_status(&self, id: i64) -> Result<BakeDocumentPayload, ApiError> {
        let document = self
            .storage
            .toggle_bake_document_status(id)?
            .ok_or_else(|| ApiError::NotFound(format!("document {id} not found")))?;
        let is_favorite = self
            .storage
            .is_memory_favorite(FAVORITE_KIND_DOCUMENT, document.id)?;
        Ok(map_document_record(document, is_favorite))
    }

    pub fn delete_document(&self, id: i64) -> Result<(), ApiError> {
        if !self.storage.soft_delete_bake_document(id)? {
            return Err(ApiError::NotFound(format!("document {id} not found")));
        }
        Ok(())
    }
    pub fn list_sops(&self) -> Result<Vec<BakeSopPayload>, ApiError> {
        let favorite_ids = self
            .storage
            .list_memory_favorite_ids(FAVORITE_KIND_OPERATION)?;
        Ok(self
            .storage
            .list_timelines_by_category(CATEGORY_BAKE_SOP)?
            .into_iter()
            .filter(is_current_bake_entry)
            .filter(|record| matches_entry_bucket(record, None))
            .map(|record| {
                let is_favorite = favorite_ids.contains(&record.id);
                let mut payload = map_sop_record_with_linked_summaries(&self.storage, record);
                payload.is_favorite = is_favorite;
                payload
            })
            .collect())
    }

    pub fn list_sops_paginated(
        &self,
        filter: BakeListFilter,
    ) -> Result<BakePagedResponse<BakeSopPayload>, ApiError> {
        let favorite_ids = self
            .storage
            .list_memory_favorite_ids(FAVORITE_KIND_OPERATION)?;
        let records = self.storage.list_timelines_by_category(CATEGORY_BAKE_SOP)?;
        let exact_id = filter.q.as_deref().and_then(parse_exact_list_id);
        let filtered_records = if let Some(id) = exact_id {
            records
                .into_iter()
                .filter(|record| record.id == id)
                .filter(is_current_bake_entry)
                .filter(|record| matches_entry_bucket(record, filter.bucket))
                .filter(|record| {
                    filter.favorite.map_or(true, |favorite| {
                        favorite_ids.contains(&record.id) == favorite
                    })
                })
                .filter(|record| {
                    filter
                        .from_ts
                        .map_or(true, |from| record.created_at_ms >= from)
                })
                .filter(|record| filter.to_ts.map_or(true, |to| record.created_at_ms <= to))
                .collect::<Vec<_>>()
        } else if let Some(query) = filter.q.as_deref() {
            let query_lower = query.to_lowercase();
            // FTS5 预筛：bake_sops_fts 候选可用时先收窄到候选 ID，再做内存 contains 校验；
            // FTS 不可用（表缺失/候选为空/被截断）时为 None，回退原有全量过滤。
            let fts_ids: Option<HashSet<i64>> = self
                .storage
                .bake_sop_fts_candidate_ids(query)
                .map(|ids| ids.into_iter().collect());
            records
                .into_iter()
                .filter(|record| {
                    fts_ids
                        .as_ref()
                        .map_or(true, |ids| ids.contains(&record.id))
                        && is_current_bake_entry(record)
                        && matches_entry_bucket(record, filter.bucket)
                        && filter.favorite.map_or(true, |favorite| {
                            favorite_ids.contains(&record.id) == favorite
                        })
                        && filter
                            .from_ts
                            .map_or(true, |from| record.created_at_ms >= from)
                        && filter.to_ts.map_or(true, |to| record.created_at_ms <= to)
                        && (record.summary.to_lowercase().contains(&query_lower)
                            || record
                                .overview
                                .as_deref()
                                .unwrap_or_default()
                                .to_lowercase()
                                .contains(&query_lower)
                            || record
                                .details
                                .as_deref()
                                .unwrap_or_default()
                                .to_lowercase()
                                .contains(&query_lower)
                            || record.category.to_lowercase().contains(&query_lower))
                })
                .collect::<Vec<_>>()
        } else {
            records
                .into_iter()
                .filter(is_current_bake_entry)
                .filter(|record| matches_entry_bucket(record, filter.bucket))
                .filter(|record| {
                    filter.favorite.map_or(true, |favorite| {
                        favorite_ids.contains(&record.id) == favorite
                    })
                })
                .filter(|record| {
                    filter
                        .from_ts
                        .map_or(true, |from| record.created_at_ms >= from)
                })
                .filter(|record| filter.to_ts.map_or(true, |to| record.created_at_ms <= to))
                .collect::<Vec<_>>()
        };
        let total = filtered_records.len() as i64;
        let items = filtered_records
            .into_iter()
            .skip(filter.offset)
            .take(filter.limit)
            .map(|record| {
                let is_favorite = favorite_ids.contains(&record.id);
                let mut payload = map_sop_record_with_linked_summaries(&self.storage, record);
                payload.is_favorite = is_favorite;
                payload
            })
            .collect();
        Ok(BakePagedResponse {
            items,
            total,
            limit: filter.limit,
            offset: filter.offset,
        })
    }

    pub fn get_sop(&self, id: i64) -> Result<BakeSopPayload, ApiError> {
        let record = self
            .storage
            .get_bake_sop(id)?
            .ok_or_else(|| ApiError::NotFound(format!("sop {id} not found")))?;
        let mut payload = map_sop_record_with_linked_summaries(
            &self.storage,
            bake_sop_record_to_timeline(record),
        );
        payload.is_favorite = self
            .storage
            .is_memory_favorite(FAVORITE_KIND_OPERATION, id)?;
        Ok(payload)
    }

    pub fn create_sop(
        &self,
        payload: CreateOrUpdateSopRequest,
    ) -> Result<BakeSopPayload, ApiError> {
        validate_sop_request(&payload)?;
        let title = payload.extracted_problem.trim().to_string();
        let steps = payload
            .steps
            .into_iter()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>();
        let trigger_keywords = payload
            .trigger_keywords
            .into_iter()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>();
        let details = json!({
            "source_timeline_id": "",
            "source_capture_id": "",
            "source_title": title.clone(),
            "trigger_keywords": trigger_keywords.clone(),
            "confidence": "medium",
            "extracted_problem": title.clone(),
            "steps": steps.clone(),
            "linked_knowledge_ids": [],
            "status": "confirmed",
            "review_status": "confirmed",
        });
        let record = NewBakeSop {
            timeline_id: 0,
            title: title.clone(),
            summary: title,
            content: Some(details.to_string()),
            detailed_content: normalize_optional_text(payload.detailed_content),
            entities: to_json_string(&trigger_keywords)?,
            importance: 5,
            source_capture_ids: Some("[]".to_string()),
        };
        let id = self.storage.insert_bake_sop(&record)?;
        self.get_sop(id)
    }

    pub fn update_sop(
        &self,
        id: i64,
        payload: CreateOrUpdateSopRequest,
    ) -> Result<BakeSopPayload, ApiError> {
        validate_sop_request(&payload)?;
        let existing = self
            .storage
            .get_bake_sop(id)?
            .ok_or_else(|| ApiError::NotFound(format!("sop {id} not found")))?;
        let title = payload.extracted_problem.trim().to_string();
        let steps = payload
            .steps
            .into_iter()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>();
        let trigger_keywords = payload
            .trigger_keywords
            .into_iter()
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>();
        let mut details = parse_details(existing.content.as_deref())
            .as_object()
            .cloned()
            .unwrap_or_default();
        details.insert(
            "trigger_keywords".to_string(),
            json!(trigger_keywords.clone()),
        );
        details.insert("extracted_problem".to_string(), json!(title.clone()));
        details.insert("steps".to_string(), json!(steps));
        details.insert("status".to_string(), json!("confirmed"));
        details.insert("review_status".to_string(), json!("confirmed"));
        if !self.storage.update_bake_sop_manual(
            id,
            &title,
            &title,
            Some(&Value::Object(details).to_string()),
            normalize_optional_text(payload.detailed_content).as_deref(),
            &to_json_string(&trigger_keywords)?,
            existing.importance,
        )? {
            return Err(ApiError::NotFound(format!("sop {id} not found")));
        }
        self.get_sop(id)
    }

    pub fn adopt_sop(&self, id: i64) -> Result<BakeSopPayload, ApiError> {
        let entry = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("sop {id} not found")))?;
        if entry.category != CATEGORY_BAKE_SOP {
            return Err(ApiError::BadRequest(format!(
                "knowledge {id} is not a bake sop"
            )));
        }
        let details = parse_details(entry.details.as_deref())
            .as_object()
            .cloned()
            .unwrap_or_default();
        let mut next_details = serde_json::Map::from_iter(details);
        next_details.insert("status".to_string(), json!("confirmed"));
        next_details.insert("review_status".to_string(), json!("confirmed"));
        let entities = entry.entities.clone();
        self.storage.update_timeline_details_system(
            id,
            &entry.summary,
            entry.overview.as_deref(),
            Some(&Value::Object(next_details).to_string()),
            &entities,
        )?;
        self.storage.set_knowledge_verified(id, true)?;
        let updated = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("sop {id} not found after update")))?;
        Ok(map_sop_record_with_linked_summaries(&self.storage, updated))
    }

    pub fn ignore_sop(&self, id: i64) -> Result<BakeSopPayload, ApiError> {
        let updated = self.update_bake_artifact_status(id, CATEGORY_BAKE_SOP, "ignored")?;
        Ok(map_sop_record_with_linked_summaries(&self.storage, updated))
    }

    pub fn delete_sop(&self, id: i64) -> Result<(), ApiError> {
        if !self.storage.delete_bake_sop(id)? {
            return Err(ApiError::NotFound(format!("sop {id} not found")));
        }
        Ok(())
    }

    pub fn list_memories(&self) -> Result<Vec<BakeMemoryPayload>, ApiError> {
        self.storage
            .list_timelines_paginated(None, 5000, 0)?
            .into_iter()
            .map(|record| self.map_memory_record_with_capture_url(record))
            .collect()
    }

    pub fn list_memories_paginated(
        &self,
        filter: BakeMemoryFilter,
    ) -> Result<BakePagedResponse<BakeMemoryPayload>, ApiError> {
        let total = self.storage.count_bake_memories_filtered(
            filter.q.as_deref(),
            filter.from_ts,
            filter.to_ts,
        )?;
        let items = self
            .storage
            .list_bake_memories_paginated(
                filter.q.as_deref(),
                filter.from_ts,
                filter.to_ts,
                filter.limit,
                filter.offset,
            )?
            .into_iter()
            .map(|record| self.map_memory_record_with_capture_url(record))
            .collect::<Result<Vec<_>, _>>()?;
        Ok(BakePagedResponse {
            items,
            total,
            limit: filter.limit,
            offset: filter.offset,
        })
    }

    pub fn list_knowledge_paginated(
        &self,
        filter: BakeListFilter,
    ) -> Result<BakePagedResponse<BakeKnowledgePayload>, ApiError> {
        let exact_id = filter.q.as_deref().and_then(parse_exact_list_id);
        let favorite_ids = self
            .storage
            .list_memory_favorite_ids(FAVORITE_KIND_KNOWLEDGE)?;
        let records = self.storage.list_bake_knowledge_paginated(None, 5000, 0)?;
        let mut filtered = records
            .into_iter()
            .filter(|record| exact_id.map_or(true, |id| record.id == id))
            .filter(is_current_bake_entry)
            .filter(|record| matches_entry_bucket(record, filter.bucket))
            .filter(|record| {
                filter.favorite.map_or(true, |favorite| {
                    favorite_ids.contains(&record.id) == favorite
                })
            })
            .filter(|record| {
                filter
                    .from_ts
                    .map_or(true, |from| record.created_at_ms >= from)
            })
            .filter(|record| filter.to_ts.map_or(true, |to| record.created_at_ms <= to))
            .map(|record| {
                let is_favorite = favorite_ids.contains(&record.id);
                self.map_knowledge_record_with_capture_url(record)
                    .map(|mut payload| {
                        payload.is_favorite = is_favorite;
                        payload
                    })
            })
            .collect::<Result<Vec<_>, _>>()?;
        if exact_id.is_none() {
            if let Some(query) = filter.q.as_deref() {
                let query_lower = query.to_lowercase();
                filtered.retain(|item| {
                    item.summary.to_lowercase().contains(&query_lower)
                        || item
                            .overview
                            .as_deref()
                            .unwrap_or_default()
                            .to_lowercase()
                            .contains(&query_lower)
                        || item
                            .detailed_content
                            .as_deref()
                            .unwrap_or_default()
                            .to_lowercase()
                            .contains(&query_lower)
                        || item.category.to_lowercase().contains(&query_lower)
                        || item
                            .source_url
                            .as_deref()
                            .unwrap_or_default()
                            .to_lowercase()
                            .contains(&query_lower)
                });
            }
        }
        if filter.sort == BakeListSort::Heat {
            filtered.sort_by(|left, right| {
                right
                    .occurrence_count
                    .cmp(&left.occurrence_count)
                    .then(right.importance.cmp(&left.importance))
                    .then(right.updated_at_ms.cmp(&left.updated_at_ms))
                    .then(left.id.cmp(&right.id))
            });
        }
        let total = filtered.len() as i64;
        let items = filtered
            .into_iter()
            .skip(filter.offset)
            .take(filter.limit)
            .collect();
        Ok(BakePagedResponse {
            items,
            total,
            limit: filter.limit,
            offset: filter.offset,
        })
    }

    pub fn get_knowledge(&self, id: i64) -> Result<BakeKnowledgePayload, ApiError> {
        let record = self
            .storage
            .get_bake_knowledge(id)?
            .ok_or_else(|| ApiError::NotFound(format!("knowledge {id} not found")))?;
        let mut payload =
            self.map_knowledge_record_with_capture_url(bake_knowledge_record_to_timeline(record))?;
        payload.is_favorite = self
            .storage
            .is_memory_favorite(FAVORITE_KIND_KNOWLEDGE, id)?;
        Ok(payload)
    }

    /// 定向查询某条时间线关联的 bake 产物（知识/文档/操作/数据）。
    /// 前端时间线详情回溯区原先拉全量列表再按 sourceTimelineId 过滤，
    /// 列表接口分页上限会截断窗口外的关联产物；这里改为按来源定向查，
    /// 过滤口径与列表接口默认 bucket 保持一致。
    pub fn get_timeline_relations(
        &self,
        timeline_id: i64,
    ) -> Result<TimelineRelationsPayload, ApiError> {
        let timeline_id_text = timeline_id.to_string();

        let knowledge = self
            .storage
            .find_bake_knowledge_by_source_timeline(timeline_id)?
            .into_iter()
            .map(bake_knowledge_record_to_timeline)
            .filter(is_current_bake_entry)
            .filter(|record| matches_entry_bucket(record, None))
            .map(|record| self.map_knowledge_record_with_capture_url(record))
            .collect::<Result<Vec<_>, _>>()?
            .into_iter()
            .find(|payload| payload.source_timeline_id == timeline_id_text)
            .map(|mut payload| {
                payload.is_favorite = self
                    .storage
                    .is_memory_favorite(
                        FAVORITE_KIND_KNOWLEDGE,
                        payload.id.parse::<i64>().unwrap_or(0),
                    )
                    .unwrap_or(false);
                payload
            });

        let document = self
            .storage
            .find_bake_document_by_source_memory_id(timeline_id)?
            .filter(is_current_bake_document)
            .filter(|record| matches_document_bucket(record, None))
            .map(|record| {
                let is_favorite = self
                    .storage
                    .is_memory_favorite(FAVORITE_KIND_DOCUMENT, record.id)
                    .unwrap_or(false);
                map_document_record(record, is_favorite)
            });

        let sop = self
            .storage
            .find_bake_sops_by_source_timeline(timeline_id)?
            .into_iter()
            .map(bake_sop_record_to_timeline)
            .filter(is_current_bake_entry)
            .filter(|record| matches_entry_bucket(record, None))
            .map(|record| map_sop_record_with_linked_summaries(&self.storage, record))
            .find(|payload| payload.source_timeline_id == timeline_id_text)
            .map(|mut payload| {
                payload.is_favorite = self
                    .storage
                    .is_memory_favorite(
                        FAVORITE_KIND_OPERATION,
                        payload.id.parse::<i64>().unwrap_or(0),
                    )
                    .unwrap_or(false);
                payload
            });

        let data = self.storage.find_data_source_by_timeline(timeline_id)?;

        Ok(TimelineRelationsPayload {
            timeline_id,
            knowledge,
            document,
            sop,
            data,
        })
    }

    pub fn create_knowledge(
        &self,
        payload: CreateOrUpdateKnowledgeRequest,
    ) -> Result<BakeKnowledgePayload, ApiError> {
        validate_knowledge_request(&payload)?;
        let summary = payload.summary.trim().to_string();
        let overview = normalize_optional_text(payload.overview).unwrap_or_else(|| summary.clone());
        let details = json!({
            "source_timeline_id": "",
            "status": "confirmed",
            "review_status": "confirmed",
        });
        let record = NewBakeKnowledge {
            timeline_id: 0,
            title: overview,
            summary,
            content: Some(details.to_string()),
            detailed_content: normalize_optional_text(payload.detailed_content),
            entities: "[]".to_string(),
            importance: payload.importance.clamp(1, 10),
            source_capture_ids: Some("[]".to_string()),
        };
        let id = self.storage.insert_bake_knowledge(&record)?;
        self.get_knowledge(id)
    }

    pub fn update_knowledge(
        &self,
        id: i64,
        payload: CreateOrUpdateKnowledgeRequest,
    ) -> Result<BakeKnowledgePayload, ApiError> {
        validate_knowledge_request(&payload)?;
        let existing = self
            .storage
            .get_bake_knowledge(id)?
            .ok_or_else(|| ApiError::NotFound(format!("knowledge {id} not found")))?;
        let summary = payload.summary.trim().to_string();
        let overview = normalize_optional_text(payload.overview).unwrap_or_else(|| summary.clone());
        let mut details = parse_details(existing.content.as_deref())
            .as_object()
            .cloned()
            .unwrap_or_default();
        details.insert("status".to_string(), json!("confirmed"));
        details.insert("review_status".to_string(), json!("confirmed"));
        if !self.storage.update_bake_knowledge_manual(
            id,
            &overview,
            &summary,
            Some(&Value::Object(details).to_string()),
            normalize_optional_text(payload.detailed_content).as_deref(),
            &existing.entities,
            payload.importance.clamp(1, 10),
        )? {
            return Err(ApiError::NotFound(format!("knowledge {id} not found")));
        }
        self.get_knowledge(id)
    }

    pub fn adopt_knowledge(&self, id: i64) -> Result<BakeKnowledgePayload, ApiError> {
        let updated = self.update_bake_artifact_status(id, CATEGORY_BAKE_KNOWLEDGE, "confirmed")?;
        self.map_knowledge_record_with_capture_url(updated)
    }

    pub fn ignore_knowledge(&self, id: i64) -> Result<BakeKnowledgePayload, ApiError> {
        let updated = self.update_bake_artifact_status(id, CATEGORY_BAKE_KNOWLEDGE, "ignored")?;
        self.map_knowledge_record_with_capture_url(updated)
    }

    pub fn delete_knowledge(&self, id: i64) -> Result<(), ApiError> {
        if !self.storage.delete_bake_knowledge(id)? {
            return Err(ApiError::NotFound(format!("knowledge {id} not found")));
        }
        Ok(())
    }

    pub fn list_capture_records_paginated(
        &self,
        filter: BakeCaptureFilter,
    ) -> Result<BakePagedResponse<BakeCapturePayload>, ApiError> {
        let mut capture_filter = crate::storage::repo::capture::CaptureFilter::new();
        capture_filter.limit = filter.limit;
        capture_filter.offset = filter.offset;
        capture_filter.from_ts = filter.from_ts;
        capture_filter.to_ts = filter.to_ts;
        capture_filter.query = filter.q;
        capture_filter.app_name = filter.app_name;
        capture_filter.capture_id = filter.source_capture_id;
        let total = self.storage.count_captures(&capture_filter)?;
        let records = self.storage.list_captures(&capture_filter)?;
        let capture_ids = records.iter().map(|record| record.id).collect::<Vec<_>>();
        let timeline_links = self.storage.list_capture_timeline_links(&capture_ids)?;
        let items = records
            .into_iter()
            .map(|record| {
                let capture_id = record.id;
                map_capture_record(record, timeline_links.get(&capture_id))
            })
            .collect();
        Ok(BakePagedResponse {
            items,
            total,
            limit: capture_filter.limit,
            offset: capture_filter.offset,
        })
    }

    pub fn get_capture_record(&self, id: i64) -> Result<BakeCapturePayload, ApiError> {
        let record = self
            .storage
            .get_capture(id)?
            .ok_or_else(|| ApiError::NotFound(format!("capture {id} not found")))?;
        let timeline_links = self.storage.list_capture_timeline_links(&[record.id])?;
        Ok(map_capture_record(record, timeline_links.get(&id)))
    }

    pub fn initialize_memories(
        &self,
        limit: usize,
    ) -> Result<InitializeBakeMemoriesResponse, ApiError> {
        // 历史版本会在 timelines 中创建 category=bake_article 的候选壳；新流程直接写入专门的 bake_* 表。
        let skipped = self
            .storage
            .list_bake_memory_init_candidates(0, limit.saturating_mul(4).max(limit))?
            .into_iter()
            .filter(is_high_value_candidate)
            .take(limit)
            .count() as i64;
        let created = Vec::new();

        Ok(InitializeBakeMemoriesResponse {
            created_count: 0,
            skipped_count: skipped,
            articles: created.clone(),
            memories: created,
        })
    }

    pub fn ignore_memory(&self, id: i64) -> Result<BakeMemoryPayload, ApiError> {
        self.update_memory_status(id, "ignored")
    }

    pub fn delete_memory(&self, id: i64) -> Result<(), ApiError> {
        if self.storage.get_episodic_memory(id)?.is_none() {
            return Err(ApiError::NotFound(format!("memory {id} not found")));
        }
        if !self.storage.delete_episodic_memory(id)? {
            return Err(ApiError::NotFound(format!("memory {id} not found")));
        }
        Ok(())
    }

    fn update_bake_artifact_status(
        &self,
        id: i64,
        expected_category: &str,
        status: &str,
    ) -> Result<TimelineRecord, ApiError> {
        let entry = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("artifact {id} not found")))?;
        if entry.category != expected_category {
            return Err(ApiError::BadRequest(format!(
                "knowledge {id} is not in category {expected_category}"
            )));
        }

        let details = parse_details(entry.details.as_deref())
            .as_object()
            .cloned()
            .unwrap_or_default();
        let mut next_details = serde_json::Map::from_iter(details);
        next_details.insert("status".to_string(), json!(status));
        next_details.insert("review_status".to_string(), json!(status));
        self.storage.update_timeline_details_system(
            id,
            &entry.summary,
            entry.overview.as_deref(),
            Some(&Value::Object(next_details).to_string()),
            &entry.entities,
        )?;
        if matches!(status, "confirmed" | "auto_created") {
            self.storage.set_knowledge_verified(id, true)?;
        }
        let updated = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("artifact {id} not found after update")))?;
        Ok(updated)
    }

    pub fn promote_memory_to_document(&self, id: i64) -> Result<BakeDocumentPayload, ApiError> {
        let memory = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("memory {id} not found")))?;
        if memory.category != CATEGORY_BAKE_ARTICLE {
            return Err(ApiError::BadRequest(format!(
                "knowledge {id} is not in category {CATEGORY_BAKE_ARTICLE}"
            )));
        }

        let payload = self.map_memory_record_with_capture_url(memory.clone())?;
        let source_memory_ids = vec![id.to_string()];
        let source_capture_ids = payload
            .source_capture_id
            .clone()
            .map(|value| vec![value])
            .unwrap_or_default();
        let linked_knowledge_ids = payload
            .source_timeline_id
            .clone()
            .map(|value| vec![value])
            .unwrap_or_default();
        let structure_sections = vec![
            DocumentSectionPayload {
                title: "可复用结构".to_string(),
                keywords: payload.tags.clone(),
                notes: memory.overview.clone(),
            },
            DocumentSectionPayload {
                title: "写作参考".to_string(),
                keywords: vec!["表达风格".to_string(), "行文脉络".to_string()],
                notes: Some("从该时间线手动沉淀，后续可继续补充章节与表达规则。".to_string()),
            },
        ];
        let detailed_content = format!(
            "## 模板价值\n\n{}\n\n## 使用建议\n\n- 参考该时间线的结构和表达方式生成新的方案、设计或汇报文档。\n- 后续可继续补充章节标题、常用表达和 AI 替代词规则。",
            memory
                .overview
                .as_deref()
                .filter(|value| !value.trim().is_empty())
                .unwrap_or(&memory.summary)
        );

        let document = NewBakeDocument {
            title: memory.summary,
            doc_type: "手动沉淀".to_string(),
            status: "enabled".to_string(),
            tags: to_json_string(&payload.tags)?,
            applicable_tasks: to_json_string(&vec![
                "方案撰写".to_string(),
                "设计文档".to_string(),
                "汇报总结".to_string(),
            ])?,
            source_memory_ids: to_json_string(&source_memory_ids)?,
            source_capture_ids: to_json_string(&source_capture_ids)?,
            source_episode_ids: to_json_string(&source_memory_ids)?,
            linked_knowledge_ids: to_json_string(&linked_knowledge_ids)?,
            sections_json: to_json_string(&structure_sections)?,
            style_phrases: "[]".to_string(),
            replacement_rules: "[]".to_string(),
            summary: None,
            full_content: Some(detailed_content),
            structured_content: "{}".to_string(),
            prompt_hint: Some("参考该时间线的结构、行文脉络和表达风格生成新文档。".to_string()),
            diagram_code: None,
            image_assets: "[]".to_string(),
            source_app_name: None,
            source_win_title: None,
            source_url: payload.url.clone(),
            content_hash: None,
            language: None,
            usage_count: 0,
            match_score: None,
            match_level: None,
            creation_mode: "manual".to_string(),
            review_status: "auto_created".to_string(),
            evidence_summary: Some("由用户从收藏时间线手动沉淀为文档。".to_string()),
            generation_version: None,
            deleted_at: None,
        };
        let document_id = self.storage.insert_bake_document(&document)?;
        let created = self
            .storage
            .get_bake_document(document_id)?
            .ok_or_else(|| {
                ApiError::NotFound(format!("document {document_id} not found after insert"))
            })?;
        Ok(map_document_record(created, false))
    }

    pub fn promote_memory_to_sop(&self, id: i64) -> Result<BakeSopPayload, ApiError> {
        let memory = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("memory {id} not found")))?;
        let payload = self.map_memory_record_with_capture_url(memory.clone())?;
        let details = json!({
            "source_capture_id": memory.capture_id.to_string(),
            "source_title": payload.title,
            "trigger_keywords": payload.tags,
            "confidence": "medium",
            "steps": ["确认问题类型", "查找关联知识", "输出标准说明"],
            "linked_knowledge_ids": [id.to_string()],
            "status": "auto_created"
        });
        let new_entry = NewTimeline {
            capture_id: memory.capture_id,
            summary: memory.summary,
            overview: memory.overview,
            details: Some(details.to_string()),
            entities: memory.entities,
            category: CATEGORY_BAKE_SOP.to_string(),
            importance: memory.importance.max(3),
            occurrence_count: memory.occurrence_count,
            observed_at: memory.observed_at,
            event_time_start: memory.event_time_start,
            event_time_end: memory.event_time_end,
            history_view: memory.history_view,
            content_origin: memory.content_origin,
            activity_type: memory.activity_type,
            is_self_generated: memory.is_self_generated,
            evidence_strength: memory.evidence_strength,
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
        };
        let sop_id = self.storage.insert_episodic_memory(&new_entry)?;
        let created = self
            .storage
            .get_timeline_entry(sop_id)?
            .ok_or_else(|| ApiError::NotFound(format!("sop {sop_id} not found after insert")))?;
        Ok(map_sop_record_with_linked_summaries(&self.storage, created))
    }

    pub async fn run_bake_pipeline(
        &self,
        trigger_reason: &str,
        limit: usize,
    ) -> Result<BakeRunPayload, ApiError> {
        let started_at = now_ms();
        let run_id = self.storage.insert_bake_run(&NewBakeRun {
            trigger_reason: trigger_reason.to_string(),
            status: "running".to_string(),
            started_at,
        })?;

        let result = self
            .execute_bake_pipeline(run_id, trigger_reason, started_at, limit, 1)
            .await;
        match result {
            Ok(payload) => Ok(payload),
            Err(err) => {
                let completed_at = now_ms();
                let latency_ms = completed_at.saturating_sub(started_at);
                if is_transient_bake_error(&err) {
                    let _ = self.storage.defer_bake_run_preserving_progress(
                        run_id,
                        completed_at,
                        &err.to_string(),
                        Some(latency_ms),
                    );
                } else {
                    let _ = self.storage.fail_bake_run_preserving_progress(
                        run_id,
                        completed_at,
                        &err.to_string(),
                        Some(latency_ms),
                    );
                }
                Err(err)
            }
        }
    }

    /// 把烤制流水线丢到独立 tokio task 跑，立即返回 run_id。
    ///
    /// 这避免了客户端（例如 ai-sidecar 的 15s urlopen 超时）关闭连接时 axum
    /// 把整个 handler future drop 掉、导致 [`Self::run_bake_pipeline`] 的
    /// match 收尾代码永远不执行、`bake_runs.status` 永远停在 `running` 的问题。
    /// 后台 task 自带 try-catch，不论 Ok/Err 都会写收尾状态。
    pub fn spawn_bake_pipeline(
        self,
        trigger_reason: String,
        limit: usize,
        extract_concurrency: usize,
    ) -> Result<i64, ApiError> {
        let started_at = now_ms();
        let run_id = self.storage.insert_bake_run(&NewBakeRun {
            trigger_reason: trigger_reason.clone(),
            status: "running".to_string(),
            started_at,
        })?;

        tokio::spawn(async move {
            // 超时预算从 bake_runs.started_at 开始计算，而不是从后台 task 首次被
            // runtime 调度时才开始。这样即使 runtime 饥饿，数据库里的总耗时也
            // 不会出现“1800 秒超时却记录成 6000 秒”的假象。
            let max_total_ms = (BAKE_RUN_MAX_TOTAL_SECS as i64) * 1000;
            let queued_ms = now_ms().saturating_sub(started_at);
            let remaining_ms = max_total_ms.saturating_sub(queued_ms);
            if remaining_ms == 0 {
                let completed_at = now_ms();
                let latency_ms = completed_at.saturating_sub(started_at);
                let message = format!(
                    "bake run expired before execution after {}ms queued",
                    queued_ms
                );
                let _ = self.storage.fail_bake_run_preserving_progress(
                    run_id,
                    completed_at,
                    &message,
                    Some(latency_ms),
                );
                tracing::error!("bake run {} {}", run_id, message);
                return;
            }

            // 用剩余总预算包裹 execute_bake_pipeline，防止任何原因导致永久挂起。
            let result = tokio::time::timeout(
                Duration::from_millis(remaining_ms as u64),
                self.execute_bake_pipeline(
                    run_id,
                    &trigger_reason,
                    started_at,
                    limit,
                    extract_concurrency,
                ),
            )
            .await;

            match result {
                Ok(Ok(_)) => {
                    tracing::info!("bake run {} completed in background", run_id);
                }
                Ok(Err(err)) => {
                    let completed_at = now_ms();
                    let latency_ms = completed_at.saturating_sub(started_at);
                    let deferred = is_transient_bake_error(&err);
                    let write_result = if deferred {
                        self.storage.defer_bake_run_preserving_progress(
                            run_id,
                            completed_at,
                            &err.to_string(),
                            Some(latency_ms),
                        )
                    } else {
                        self.storage.fail_bake_run_preserving_progress(
                            run_id,
                            completed_at,
                            &err.to_string(),
                            Some(latency_ms),
                        )
                    };
                    if let Err(write_err) = write_result {
                        tracing::error!(
                            "bake run {} terminal status write failed: deferred={} err={} write_err={}",
                            run_id,
                            deferred,
                            err,
                            write_err
                        );
                    } else if deferred {
                        tracing::warn!(
                            "bake run {} deferred after transient upstream interruption: {}",
                            run_id,
                            err
                        );
                    } else {
                        tracing::error!("bake run {} failed in background: {}", run_id, err);
                    }
                }
                Err(_elapsed) => {
                    let completed_at = now_ms();
                    let latency_ms = completed_at.saturating_sub(started_at);
                    tracing::error!(
                        "bake run {} timed out after {}s, forcing failed status",
                        run_id,
                        BAKE_RUN_MAX_TOTAL_SECS
                    );
                    let timeout_message =
                        format!("bake run timed out after {}s", BAKE_RUN_MAX_TOTAL_SECS);
                    let write_result = self.storage.fail_bake_run_preserving_progress(
                        run_id,
                        completed_at,
                        &timeout_message,
                        Some(latency_ms),
                    );
                    if let Err(write_err) = write_result {
                        tracing::error!(
                            "bake run {} timeout cleanup failed: write_err={}",
                            run_id,
                            write_err
                        );
                    }
                }
            }
        });

        Ok(run_id)
    }

    async fn execute_bake_pipeline(
        &self,
        run_id: i64,
        trigger_reason: &str,
        started_at: i64,
        limit: usize,
        extract_concurrency: usize,
    ) -> Result<BakeRunPayload, ApiError> {
        let extract_concurrency = extract_concurrency.clamp(1, 3);

        tracing::info!(
            "bake run {} execute_bake_pipeline start concurrency={}",
            run_id,
            extract_concurrency
        );

        // document 去重仍需全量（需要 URL 去重 + source_episode_ids JSON 解析），沿用原逻辑
        let existing_documents = self.storage.list_bake_documents()?;
        let watermark = self
            .storage
            .get_bake_watermark(UNIFIED_BAKE_PIPELINE_NAME)?;
        let mut existing_document_sources =
            collect_current_document_source_timeline_ids(&existing_documents);
        let mut existing_document_urls: std::collections::HashSet<String> = existing_documents
            .iter()
            .filter_map(|d| d.source_url.as_deref().map(normalize_doc_url))
            .filter(|s| !s.is_empty())
            .collect();
        let mut max_processed_ts = watermark
            .as_ref()
            .map(|item| item.last_processed_ts)
            .unwrap_or(0);
        let initial_watermark_ts = max_processed_ts;

        let scan_limit = limit.saturating_mul(6).max(limit);
        let mut fresh_candidates = self.storage.list_bake_memory_fresh_candidates(
            max_processed_ts,
            scan_limit,
            MAX_BAKE_RETRY_FAILURES,
        )?;
        let retry_candidates = self
            .storage
            .list_bake_memory_retry_candidates(scan_limit, MAX_BAKE_RETRY_FAILURES)?;

        // Watermark 自动回退：如果 watermark 已超过所有现有 timeline 的 updated_at_ms，
        // 导致候选列表为空（真正有 pending 的情况下），则把 watermark 重置为 0 重新扫描全量。
        // 修复：同时检查 knowledge / sop / document 三类，避免 document 候选被误判为"无 pending"。
        if fresh_candidates.is_empty() && retry_candidates.is_empty() && max_processed_ts > 0 {
            let probe =
                self.storage
                    .list_bake_memory_fresh_candidates(0, 1, MAX_BAKE_RETRY_FAILURES)?;
            let probe_ids: Vec<i64> = probe.iter().map(|c| c.timeline.id).collect();
            let probe_knowledge = self
                .storage
                .find_existing_knowledge_timeline_ids(&probe_ids)
                .unwrap_or_default();
            let probe_sop = self
                .storage
                .find_existing_sop_timeline_ids(&probe_ids)
                .unwrap_or_default();
            let any_pending = probe.iter().any(|c| {
                !probe_sop.contains(&c.timeline.id)
                    && !probe_knowledge.contains(&c.timeline.id)
                    && !existing_document_sources.contains(&c.timeline.id)
            });
            if any_pending {
                tracing::info!(
                    "bake watermark reset: watermark={} 已超过所有候选，自动回退到 0 重新扫描",
                    max_processed_ts
                );
                max_processed_ts = 0;
                fresh_candidates = self.storage.list_bake_memory_fresh_candidates(
                    0,
                    scan_limit,
                    MAX_BAKE_RETRY_FAILURES,
                )?;
            }
        }

        // 两条 lane 分开查询、按 4:1 交错。旧重试不再占满 LIMIT，也不会因
        // watermark 已越过自身而被丢弃；新任务和重试任务都能持续取得配额。
        let candidates =
            merge_bake_candidate_lanes(fresh_candidates, retry_candidates, scan_limit, run_id);

        // 增量查询：只针对本批候选的 timeline_id 集合查已有 knowledge/sop，
        // 避免全量拉取 500 条导致随数据增长内存和时间开销膨胀。
        let candidate_timeline_ids: Vec<i64> = candidates.iter().map(|c| c.timeline.id).collect();
        let mut existing_knowledge_sources = self
            .storage
            .find_existing_knowledge_timeline_ids(&candidate_timeline_ids)
            .map_err(|e| ApiError::Internal(format!("查询已有 knowledge 失败: {e}")))?;
        let mut existing_sop_sources = self
            .storage
            .find_existing_sop_timeline_ids(&candidate_timeline_ids)
            .map_err(|e| ApiError::Internal(format!("查询已有 sop 失败: {e}")))?;

        // 候选严格按 updated_at_ms 顺序执行。Skip 与 Extract 处于同一有序队列，
        // watermark 只能在对应项真正完成后推进，不能先跨过仍在推理的候选。
        enum BakeWorkItem {
            Skip {
                timeline_id: i64,
                candidate_ts: i64,
                clear_retry: bool,
            },
            Extract(BakeMemorySourceRecord),
        }
        enum BakeWorkResult {
            Skipped {
                timeline_id: i64,
                candidate_ts: i64,
                clear_retry: bool,
            },
            Extracted(
                BakeMemorySourceRecord,
                Result<BakeExtractResponse, ApiError>,
            ),
        }

        let mut work_queue: Vec<BakeWorkItem> = Vec::new();
        let mut metadata_refresh_count = 0_usize;
        let mut queued_document_urls = std::collections::HashSet::new();
        // URL 去重被跳过的候选：已有文档时已当场登记；无文档的留给本轮先入队候选
        // 创建，待产物全部落盘后统一补登记，避免 capture/timeline 漏进文档来源。
        let mut deferred_coalesce_sources: Vec<BakeMemorySourceRecord> = Vec::new();
        let mut initial_candidate_count = 0_i64;

        for candidate in candidates {
            if work_queue.len() + metadata_refresh_count >= limit {
                break;
            }
            let candidate_ts = candidate.timeline.updated_at_ms;

            // 已有文档的来源元数据是本地确定性信息，不应依赖 sidecar 是否接受内容合并。
            // 即使全局 watermark 已越过该 timeline，也要先补齐后来追加的 capture 和 URL。
            if existing_document_sources.contains(&candidate.timeline.id) {
                if let Some(existing_doc) = self
                    .storage
                    .find_bake_document_by_source_memory_id(candidate.timeline.id)?
                {
                    if self.refresh_document_source_metadata(&candidate, &existing_doc)? {
                        tracing::info!(
                            "bake document source metadata refreshed: timeline_id={} doc_id={} source_url={:?}",
                            candidate.timeline.id,
                            existing_doc.id,
                            candidate.capture_url,
                        );
                    } else {
                        // refresh 无变化却仍被 queue-status 计入 metadata_refresh 时，
                        // 说明两边口径漂移；打出缺失 capture 差集供定位。
                        let expected = collect_source_capture_id_strings(&self.storage, &candidate)
                            .unwrap_or_default();
                        let covered: std::collections::HashSet<String> =
                            parse_json_vec_string(&existing_doc.source_capture_ids)
                                .into_iter()
                                .collect();
                        let missing: Vec<String> = expected
                            .iter()
                            .filter(|id| !covered.contains(id.as_str()))
                            .cloned()
                            .collect();
                        if !missing.is_empty() {
                            tracing::warn!(
                                "bake document source metadata refresh no-op but captures uncovered: timeline_id={} doc_id={} missing_capture_ids={:?}",
                                candidate.timeline.id,
                                existing_doc.id,
                                missing,
                            );
                        }
                    }
                }
                if candidate_ts <= max_processed_ts {
                    metadata_refresh_count += 1;
                    continue;
                }
            }

            if candidate_ts <= max_processed_ts && candidate.retry_failure_count == 0 {
                continue;
            }
            if !is_high_value_candidate(&candidate) {
                tracing::info!(
                    "bake skip: timeline_id={} importance={} evidence={:?} activity={:?} origin={:?} history_view={} self_generated={} document_candidate={} reason=not_high_value",
                    candidate.timeline.id,
                    candidate.timeline.importance,
                    candidate.timeline.evidence_strength,
                    candidate.timeline.activity_type,
                    candidate.timeline.content_origin,
                    candidate.timeline.history_view,
                    candidate.timeline.is_self_generated,
                    is_substantive_document_candidate(&candidate),
                );
                self.storage
                    .upsert_bake_candidate_audit(&new_bake_candidate_audit(
                        run_id,
                        &candidate,
                        "skipped",
                        Some("not_high_value"),
                    ))?;
                work_queue.push(BakeWorkItem::Skip {
                    timeline_id: candidate.timeline.id,
                    candidate_ts,
                    clear_retry: candidate.retry_failure_count > 0,
                });
                continue;
            }
            // 指纹预筛：候选源文本与已烘焙内容完全一致，且可能产出的产物类型
            // 都已有同指纹产物时，再调 LLM 只会得到 no_change，直接跳过并推进水位。
            if self.candidate_fully_covered_by_fingerprints(&candidate) {
                tracing::info!(
                    "bake skip: timeline_id={} reason=fingerprint_unchanged",
                    candidate.timeline.id,
                );
                self.storage
                    .upsert_bake_candidate_audit(&new_bake_candidate_audit(
                        run_id,
                        &candidate,
                        "skipped",
                        Some("fingerprint_unchanged"),
                    ))?;
                work_queue.push(BakeWorkItem::Skip {
                    timeline_id: candidate.timeline.id,
                    candidate_ts,
                    clear_retry: candidate.retry_failure_count > 0,
                });
                continue;
            }
            if let Some(document_url) = substantive_document_url(&candidate) {
                if !reserve_document_task(&candidate, &mut queued_document_urls) {
                    tracing::info!(
                        "bake coalesce: timeline_id={} canonical_url={} reason=document_url_already_queued",
                        candidate.timeline.id,
                        document_url,
                    );
                    // 合并跳过只免掉重复提炼，不能免掉来源登记
                    self.register_skipped_document_candidate_source(
                        &candidate,
                        &document_url,
                        &mut deferred_coalesce_sources,
                    )?;
                    self.storage
                        .upsert_bake_candidate_audit(&new_bake_candidate_audit(
                            run_id,
                            &candidate,
                            "skipped",
                            Some("document_url_already_queued"),
                        ))?;
                    work_queue.push(BakeWorkItem::Skip {
                        timeline_id: candidate.timeline.id,
                        candidate_ts,
                        clear_retry: candidate.retry_failure_count > 0,
                    });
                    continue;
                }
            }
            initial_candidate_count += 1;
            self.storage
                .upsert_bake_candidate_audit(&new_bake_candidate_audit(
                    run_id, &candidate, "queued", None,
                ))?;
            work_queue.push(BakeWorkItem::Extract(candidate));
        }

        let _ = self
            .storage
            .update_bake_run_progress(run_id, initial_candidate_count, 0);

        // `buffered` 最多并行轮询 N 个 extract，但严格按输入顺序产出结果。
        // 这些 future 不会 detach；整轮超时或提前返回时，drop stream 会取消所有
        // 尚未完成的 HTTP 请求，避免旧任务继续占用 sidecar 队列并拖累下一轮。
        let trigger_reason_owned = trigger_reason.to_string();
        let work_stream = futures::stream::iter(work_queue.into_iter().map(|work_item| {
            let service = self.clone();
            let reason = trigger_reason_owned.clone();
            async move {
                match work_item {
                    BakeWorkItem::Skip {
                        timeline_id,
                        candidate_ts,
                        clear_retry,
                    } => BakeWorkResult::Skipped {
                        timeline_id,
                        candidate_ts,
                        clear_retry,
                    },
                    BakeWorkItem::Extract(candidate) => {
                        tracing::info!(
                            "bake process: timeline_id={} importance={} evidence={:?} activity={:?} category={} summary_head={:?}",
                            candidate.timeline.id,
                            candidate.timeline.importance,
                            candidate.timeline.evidence_strength,
                            candidate.timeline.activity_type,
                            candidate.timeline.category,
                            candidate.timeline.summary.chars().take(40).collect::<String>(),
                        );
                        let result = service.extract_candidate(&reason, &candidate).await;
                        BakeWorkResult::Extracted(candidate, result)
                    }
                }
            }
        }))
        .buffered(extract_concurrency);
        tokio::pin!(work_stream);

        // 按顺序 await 并串行 persist（保证 HashSet 一致性 & watermark 单调）
        let mut processed_episode_count = 0_i64;
        let mut auto_created_count = 0_i64;
        let mut candidate_count = 0_i64;
        let mut discarded_count = 0_i64;
        let mut knowledge_created_count = 0_i64;
        let mut document_created_count = 0_i64;
        let mut sop_created_count = 0_i64;

        while let Some(work_result) = work_stream.next().await {
            let (candidate, extract_result) = match work_result {
                BakeWorkResult::Skipped {
                    timeline_id,
                    candidate_ts,
                    clear_retry,
                } => {
                    if clear_retry {
                        self.storage.clear_bake_retry_failure(timeline_id)?;
                    }
                    let next = max_processed_ts.max(candidate_ts);
                    if next != max_processed_ts {
                        max_processed_ts = next;
                        self.storage
                            .upsert_bake_watermark(UNIFIED_BAKE_PIPELINE_NAME, next)?;
                    }
                    continue;
                }
                BakeWorkResult::Extracted(candidate, extract_result) => (candidate, extract_result),
            };

            let extracted = match extract_result {
                Ok(v) => v,
                Err(err) => {
                    // 前台推理抢占、限流和明确的服务不可用只会延后本批。
                    // 不写候选死信，也不推进 watermark，下一批从同一候选继续。
                    if is_untracked_transient_bake_error(&err) {
                        self.storage.finalize_bake_candidate_audit(
                            run_id,
                            candidate.timeline.id,
                            "deferred",
                            Some(bake_retry_error_code(&err)),
                        )?;
                        return Err(err);
                    }
                    let count = self
                        .storage
                        .bump_bake_retry_failure_with_code(
                            candidate.timeline.id,
                            &bake_retry_failure_summary(&err),
                            bake_retry_error_code(&err),
                        )
                        .unwrap_or(0);
                    if is_retryable_bake_candidate_error(&err) && count < MAX_BAKE_RETRY_FAILURES {
                        // 单候选超时/输出非法不应中断整批：推进 watermark 越过它，
                        // 继续处理后续候选。该候选保留在 bake_retry_state，由候选
                        // 查询的重试分支在后续 run 重新捞回，直到成功或达到上限。
                        tracing::warn!(
                            "bake extract deferred for bounded retry: timeline_id={} failure_count={} max_failures={} watermark_advancing=true err={}",
                            candidate.timeline.id,
                            count,
                            MAX_BAKE_RETRY_FAILURES,
                            err
                        );
                        let next = max_processed_ts.max(candidate.timeline.updated_at_ms);
                        if next != max_processed_ts {
                            max_processed_ts = next;
                            self.storage
                                .upsert_bake_watermark(UNIFIED_BAKE_PIPELINE_NAME, next)?;
                        }
                        processed_episode_count += 1;
                        let _ = self.storage.update_bake_run_progress(
                            run_id,
                            initial_candidate_count,
                            processed_episode_count,
                        );
                        self.storage.finalize_bake_candidate_audit(
                            run_id,
                            candidate.timeline.id,
                            "retry_scheduled",
                            Some(bake_retry_error_code(&err)),
                        )?;
                        continue;
                    }
                    tracing::error!(
                        "bake extract permanently failed after bounded retry: timeline_id={} failure_count={} timeout={} err={}",
                        candidate.timeline.id,
                        count,
                        is_bake_candidate_timeout(&err),
                        err
                    );
                    let next = max_processed_ts.max(candidate.timeline.updated_at_ms);
                    if next != max_processed_ts {
                        max_processed_ts = next;
                        self.storage
                            .upsert_bake_watermark(UNIFIED_BAKE_PIPELINE_NAME, next)?;
                    }
                    discarded_count += 1;
                    processed_episode_count += 1;
                    self.storage.finalize_bake_candidate_audit(
                        run_id,
                        candidate.timeline.id,
                        "failed",
                        Some(bake_retry_error_code(&err)),
                    )?;
                    let _ = self.storage.update_bake_run_progress(
                        run_id,
                        initial_candidate_count,
                        processed_episode_count,
                    );
                    continue;
                }
            };
            tracing::info!(
                "bake bundle contract: timeline_id={} degraded={} artifact_shapes={} compatibility_recovered={}",
                candidate.timeline.id,
                extracted.degraded.unwrap_or(false),
                extracted
                    .artifact_shapes
                    .as_ref()
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "{}".to_string()),
                extracted
                    .compatibility_recovered
                    .as_ref()
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "{}".to_string()),
            );
            let sop_payload_valid = if extracted.sop.accepted {
                extracted.sop.payload.as_ref().map(|payload| {
                    serde_json::from_value::<BakeSopArtifactPayload>(payload.clone())
                        .ok()
                        .is_some_and(|parsed| {
                            validate_bake_sop_evidence(
                                &parsed,
                                &candidate,
                                &source_capture_id_strings(&candidate),
                            )
                            .is_ok()
                        })
                })
            } else {
                None
            };
            self.record_artifact_extraction_audits(run_id, &candidate, &extracted)?;
            self.storage.update_bake_candidate_audit_model(
                run_id,
                candidate.timeline.id,
                extracted.primary_type.as_deref(),
                extracted.classification_reason.as_deref(),
                extracted.sop.accepted,
                extracted.sop.reason.as_deref(),
                sop_payload_valid,
            )?;
            let candidate_result = match self
                .persist_extracted_candidate(
                    Some(run_id),
                    None,
                    &candidate,
                    trigger_reason,
                    extracted,
                    &mut existing_knowledge_sources,
                    &mut existing_document_sources,
                    &mut existing_document_urls,
                    &mut existing_sop_sources,
                )
                .await
            {
                Ok(r) => r,
                Err(err) => {
                    // 文档合并同样会经过 sidecar，可能在持久化阶段被前台任务抢占。
                    // 此时本地已落盘的部分产物保持幂等，候选留给下一批补齐。
                    if is_untracked_transient_bake_error(&err) {
                        self.storage.finalize_bake_candidate_audit(
                            run_id,
                            candidate.timeline.id,
                            "deferred",
                            Some(bake_retry_error_code(&err)),
                        )?;
                        return Err(err);
                    }
                    let count = self
                        .storage
                        .bump_bake_retry_failure_with_code(
                            candidate.timeline.id,
                            &bake_retry_failure_summary(&err),
                            bake_retry_error_code(&err),
                        )
                        .unwrap_or(0);
                    if is_retryable_bake_candidate_error(&err) && count < MAX_BAKE_RETRY_FAILURES {
                        tracing::warn!(
                            "bake persist deferred for bounded retry: timeline_id={} failure_count={} max_failures={} err={}",
                            candidate.timeline.id,
                            count,
                            MAX_BAKE_RETRY_FAILURES,
                            err
                        );
                        let next = max_processed_ts.max(candidate.timeline.updated_at_ms);
                        if next != max_processed_ts {
                            max_processed_ts = next;
                            self.storage
                                .upsert_bake_watermark(UNIFIED_BAKE_PIPELINE_NAME, next)?;
                        }
                        processed_episode_count += 1;
                        let _ = self.storage.update_bake_run_progress(
                            run_id,
                            initial_candidate_count,
                            processed_episode_count,
                        );
                        self.storage.finalize_bake_candidate_audit(
                            run_id,
                            candidate.timeline.id,
                            "retry_scheduled",
                            Some(bake_retry_error_code(&err)),
                        )?;
                        continue;
                    }
                    tracing::error!(
                        "bake persist permanently failed after bounded retry: timeline_id={} failure_count={} timeout={} err={}",
                        candidate.timeline.id,
                        count,
                        is_bake_candidate_timeout(&err),
                        err
                    );
                    let next = max_processed_ts.max(candidate.timeline.updated_at_ms);
                    if next != max_processed_ts {
                        max_processed_ts = next;
                        self.storage
                            .upsert_bake_watermark(UNIFIED_BAKE_PIPELINE_NAME, next)?;
                    }
                    discarded_count += 1;
                    processed_episode_count += 1;
                    self.storage.finalize_bake_candidate_audit(
                        run_id,
                        candidate.timeline.id,
                        "failed",
                        Some(bake_retry_error_code(&err)),
                    )?;
                    let _ = self.storage.update_bake_run_progress(
                        run_id,
                        initial_candidate_count,
                        processed_episode_count,
                    );
                    continue;
                }
            };

            self.storage.finalize_bake_candidate_audit(
                run_id,
                candidate.timeline.id,
                candidate_result
                    .sop_persist_status
                    .unwrap_or("not_evaluated"),
                candidate_result.sop_persist_reason.as_deref(),
            )?;

            auto_created_count += candidate_result.auto_created_count;
            candidate_count += candidate_result.candidate_count;
            discarded_count += candidate_result.discarded_count;
            knowledge_created_count += candidate_result.knowledge_created_count;
            document_created_count += candidate_result.document_created_count;
            sop_created_count += candidate_result.sop_created_count;
            self.storage
                .clear_bake_retry_failure(candidate.timeline.id)?;
            let next = max_processed_ts.max(candidate.timeline.updated_at_ms);
            if next != max_processed_ts {
                max_processed_ts = next;
                self.storage
                    .upsert_bake_watermark(UNIFIED_BAKE_PIPELINE_NAME, next)?;
            }
            // 只有产物和 watermark 都成功落盘后才计入实时进度。若中途被 P0
            // 抢占，deferred run 展示的是可从断点继续的真实完成数。
            processed_episode_count += 1;
            let _ = self.storage.update_bake_run_progress(
                run_id,
                initial_candidate_count,
                processed_episode_count,
            );
        }

        // URL 去重跳过的候选补登记：此时同 URL 先入队候选的文档（如有）已落盘。
        // 文档仍未创建的（先入队候选提炼失败等）不登记，靠下一轮的来源刷新兜底。
        for candidate in deferred_coalesce_sources {
            let Some(document_url) = substantive_document_url(&candidate) else {
                continue;
            };
            let Some(existing_doc) = self.storage.find_document_by_source_url(&document_url)?
            else {
                tracing::info!(
                    "bake coalesce: deferred registration skipped, no document yet timeline_id={} canonical_url={}",
                    candidate.timeline.id,
                    document_url,
                );
                continue;
            };
            if self.refresh_document_source_metadata(&candidate, &existing_doc)? {
                tracing::info!(
                    "bake coalesce: deferred candidate registered into document timeline_id={} doc_id={}",
                    candidate.timeline.id,
                    existing_doc.id,
                );
            }
        }

        let completed_at = now_ms();
        let latency_ms = completed_at.saturating_sub(started_at);
        let final_status = if processed_episode_count == 0
            && metadata_refresh_count == 0
            && max_processed_ts == initial_watermark_ts
        {
            "no_op"
        } else {
            "completed"
        };
        self.storage.complete_bake_run(
            run_id,
            final_status,
            completed_at,
            processed_episode_count,
            auto_created_count,
            candidate_count,
            discarded_count,
            knowledge_created_count,
            document_created_count,
            sop_created_count,
            None,
            Some(latency_ms),
        )?;
        let sop_funnel = self.storage.get_bake_run_sop_funnel_summary(run_id)?;
        tracing::info!(
            "bake sop funnel: run_id={} audited={} eligible={} model_accepted={} payload_valid={} persisted={}",
            run_id,
            sop_funnel.audited_count,
            sop_funnel.eligible_count,
            sop_funnel.model_accepted_count,
            sop_funnel.payload_valid_count,
            sop_funnel.persisted_count,
        );
        if sop_funnel.eligible_count >= BAKE_SOP_ZERO_OUTPUT_ELIGIBLE_ALERT_THRESHOLD
            && sop_funnel.persisted_count == 0
        {
            tracing::warn!(
                "bake sop zero-output alert: run_id={} eligible={} model_accepted={} payload_valid={} threshold={}",
                run_id,
                sop_funnel.eligible_count,
                sop_funnel.model_accepted_count,
                sop_funnel.payload_valid_count,
                BAKE_SOP_ZERO_OUTPUT_ELIGIBLE_ALERT_THRESHOLD,
            );
        }
        let latest = self.storage.get_latest_bake_run()?.ok_or_else(|| {
            ApiError::NotFound(format!("bake run {run_id} not found after completion"))
        })?;
        Ok(map_bake_run_record(latest))
    }

    async fn extract_candidate(
        &self,
        trigger_reason: &str,
        candidate: &BakeMemorySourceRecord,
    ) -> Result<BakeExtractResponse, ApiError> {
        let url = format!("{}/bake/extract", self.sidecar_url);
        let request_body = BakeExtractRequest {
            trigger_reason: trigger_reason.to_string(),
            retry_attempt: candidate.retry_failure_count,
            retry_error_code: candidate.retry_error_code.clone(),
            candidate: map_extract_candidate_payload(candidate),
        };

        let response = self
            .client
            .post(&url)
            .json(&request_body)
            .timeout(Duration::from_secs(BAKE_SIDECAR_TIMEOUT_SECS))
            .send()
            .await
            .map_err(map_sidecar_request_error)?;

        if response.status().is_success() {
            response.json::<BakeExtractResponse>().await.map_err(|err| {
                tracing::warn!("解析 bake sidecar 成功响应失败: {}", err);
                ApiError::Upstream {
                    status: StatusCode::BAD_GATEWAY,
                    code: "BAKE_SIDECAR_RESPONSE_INVALID",
                    message: "bake 提炼服务返回了无法解析的响应".to_string(),
                }
            })
        } else {
            let status = response.status();
            let body_text = response.text().await.unwrap_or_default();
            let status = StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
            let error = map_sidecar_error(status, body_text, "bake 提炼服务");
            tracing::warn!(
                "bake sidecar 返回错误 status={} code={}",
                status,
                error.code
            );
            Err(ApiError::Upstream {
                status: error.status,
                code: error.code,
                message: error.message,
            })
        }
    }

    fn document_already_persisted(
        &self,
        candidate: &BakeMemorySourceRecord,
    ) -> Result<bool, ApiError> {
        if self
            .storage
            .find_bake_artifact_by_source_timeline("document", candidate.timeline.id)?
            .is_some()
        {
            return Ok(true);
        }
        if let Some(url) = candidate.capture_url.as_deref() {
            if self.storage.find_document_by_source_url(url)?.is_some() {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn persisted_artifact_id(
        &self,
        artifact_kind: &str,
        timeline_id: i64,
    ) -> Result<Option<i64>, ApiError> {
        if artifact_kind == "document" {
            return Ok(self
                .storage
                .find_bake_document_by_source_memory_id(timeline_id)?
                .map(|document| document.id));
        }
        Ok(self
            .storage
            .find_bake_artifact_by_source_timeline(artifact_kind, timeline_id)?)
    }

    fn record_artifact_extraction_audits(
        &self,
        run_id: i64,
        candidate: &BakeMemorySourceRecord,
        extracted: &BakeExtractResponse,
    ) -> Result<(), ApiError> {
        let document_evidence = document_evidence(candidate);
        let sop_eligibility = sop_eligibility(candidate);
        let shapes = extracted
            .artifact_shapes
            .as_ref()
            .and_then(Value::as_object);
        let recovered = extracted
            .compatibility_recovered
            .as_ref()
            .and_then(Value::as_object);
        for (artifact_kind, extraction, deterministic_eligible, deterministic_reason) in [
            ("knowledge", &extracted.knowledge, None, None),
            (
                "document",
                &extracted.document,
                Some(document_evidence.allows_auto_create),
                Some(format!("{:?}", document_evidence.kind).to_lowercase()),
            ),
            (
                "sop",
                &extracted.sop,
                Some(sop_eligibility.eligible),
                Some(sop_eligibility.reason.to_string()),
            ),
        ] {
            let payload_valid = extraction
                .payload
                .as_ref()
                .map(|payload| match artifact_kind {
                    "knowledge" => parse_bake_knowledge_payload(payload.clone(), candidate).is_ok(),
                    "document" => parse_bake_document_payload(payload.clone(), candidate).is_ok(),
                    "sop" => parse_bake_sop_payload(payload.clone(), candidate)
                        .ok()
                        .is_some_and(|parsed| {
                            validate_bake_sop_evidence(
                                &parsed,
                                candidate,
                                &source_capture_id_strings(candidate),
                            )
                            .is_ok()
                        }),
                    _ => false,
                });
            let sidecar_key = if artifact_kind == "document" {
                "design"
            } else {
                artifact_kind
            };
            let artifact_shape = shapes
                .and_then(|items| items.get(sidecar_key))
                .and_then(Value::as_str)
                .map(ToString::to_string);
            let compatibility_recovered = recovered
                .and_then(|items| items.get(sidecar_key))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            tracing::info!(
                "bake artifact decision: run_id={} timeline_id={} artifact={} deterministic_eligible={:?} deterministic_reason={:?} model_accepted={} model_reason={:?} payload_present={} payload_valid={:?} artifact_shape={:?} compatibility_recovered={}",
                run_id,
                candidate.timeline.id,
                artifact_kind,
                deterministic_eligible,
                deterministic_reason,
                extraction.accepted,
                extraction.reason,
                extraction.payload.is_some(),
                payload_valid,
                artifact_shape,
                compatibility_recovered,
            );
            self.storage
                .upsert_bake_artifact_audit(&NewBakeArtifactAudit {
                    run_id,
                    timeline_id: candidate.timeline.id,
                    artifact_kind: artifact_kind.to_string(),
                    deterministic_eligible,
                    deterministic_reason,
                    model_accepted: extraction.accepted,
                    model_reason: extraction.reason.clone(),
                    payload_present: extraction.payload.is_some(),
                    payload_valid,
                    artifact_shape,
                    compatibility_recovered,
                })?;
        }
        Ok(())
    }

    fn finalize_artifact_audit(
        &self,
        run_id: Option<i64>,
        timeline_id: i64,
        artifact_kind: &str,
        extraction: &BakeArtifactExtraction,
        outcome: &CandidatePersistResult,
    ) -> Result<(), ApiError> {
        let Some(run_id) = run_id else {
            return Ok(());
        };
        let created_count = match artifact_kind {
            "knowledge" => outcome.knowledge_created_count,
            "document" => outcome.document_created_count,
            "sop" => outcome.sop_created_count,
            _ => 0,
        };
        let (status, reason) = if !extraction.accepted {
            (
                "rejected",
                extraction.reason.as_deref().or(Some("model_rejected")),
            )
        } else if created_count > 0 {
            ("created", Some("created"))
        } else {
            ("reused_or_merged", Some("source_already_persisted"))
        };
        let artifact_id = if matches!(status, "created" | "reused_or_merged") {
            self.persisted_artifact_id(artifact_kind, timeline_id)?
        } else {
            None
        };
        if artifact_kind == "knowledge" {
            if let Some(decision_state) = outcome.knowledge_decision_state {
                let decision_status = match decision_state {
                    "shadow" => "shadow",
                    "timeline_only" => "timeline_only",
                    _ => status,
                };
                let decision_reason = outcome.knowledge_decision_reason_code.or(reason);
                self.storage.finalize_bake_artifact_audit_decision(
                    run_id,
                    timeline_id,
                    artifact_kind,
                    decision_status,
                    decision_reason,
                    artifact_id,
                    Some(decision_state),
                    outcome.knowledge_quality_score,
                    outcome.knowledge_decision_reason_code,
                    outcome.knowledge_decision_reason_summary.as_deref(),
                    Some(KNOWLEDGE_DECISION_RULE_VERSION),
                    outcome.knowledge_shadow_payload_json.as_deref(),
                )?;
                return Ok(());
            }
        }
        tracing::info!(
            "bake artifact persistence: run_id={} timeline_id={} artifact={} status={} reason={:?} artifact_id={:?}",
            run_id,
            timeline_id,
            artifact_kind,
            status,
            reason,
            artifact_id,
        );
        self.storage.finalize_bake_artifact_audit(
            run_id,
            timeline_id,
            artifact_kind,
            status,
            reason,
            artifact_id,
        )?;
        Ok(())
    }

    async fn persist_extracted_candidate(
        &self,
        audit_run_id: Option<i64>,
        memory_id: Option<i64>,
        candidate: &BakeMemorySourceRecord,
        trigger_reason: &str,
        extracted: BakeExtractResponse,
        existing_knowledge_sources: &mut std::collections::HashSet<i64>,
        existing_document_sources: &mut std::collections::HashSet<i64>,
        existing_document_urls: &mut std::collections::HashSet<String>,
        existing_sop_sources: &mut std::collections::HashSet<i64>,
    ) -> Result<CandidatePersistResult, ApiError> {
        let mut result = CandidatePersistResult::default();
        let mut completed_artifacts = 0_i64;
        let mut first_error: Option<ApiError> = None;
        let mut document_persist_error_code: Option<String> = None;
        let model_document_payload_valid = extracted.document.accepted
            && extracted.document.payload.as_ref().is_some_and(|payload| {
                parse_bake_document_payload(payload.clone(), candidate).is_ok()
            });
        let deterministic_document_recovery = if audit_run_id.is_some()
            && candidate.retry_failure_count >= MAX_BAKE_RETRY_FAILURES - 1
            && document_evidence(candidate).allows_auto_create
            && !model_document_payload_valid
            && !self.document_already_persisted(candidate)?
        {
            deterministic_document_recovery(candidate)
        } else {
            None
        };
        let document_extraction = deterministic_document_recovery
            .as_ref()
            .unwrap_or(&extracted.document);

        // SOP is both the shortest artifact and the one historically lost when a
        // preceding long knowledge/document payload was malformed. Persist each
        // envelope independently; one invalid sibling must not roll back or skip
        // the others, and must not trigger another model bundle call.
        match self.persist_sop_artifact(
            memory_id,
            candidate,
            trigger_reason,
            &extracted.sop,
            existing_sop_sources,
        ) {
            Ok(outcome) => {
                result.apply(outcome.clone());
                self.finalize_artifact_audit(
                    audit_run_id,
                    candidate.timeline.id,
                    "sop",
                    &extracted.sop,
                    &outcome,
                )?;
                completed_artifacts += 1;
            }
            Err(error) => {
                let reason = format!("artifact_error:{}", bake_retry_error_code(&error));
                tracing::warn!(
                    "bake artifact persist failed independently: timeline_id={} artifact=sop error={}",
                    candidate.timeline.id,
                    error,
                );
                if let Some(run_id) = audit_run_id {
                    self.storage.finalize_bake_artifact_audit(
                        run_id,
                        candidate.timeline.id,
                        "sop",
                        "failed",
                        Some(bake_retry_error_code(&error)),
                        None,
                    )?;
                }
                result.sop_persist_status = Some("failed");
                result.sop_persist_reason = Some(reason);
                first_error = Some(error);
            }
        }

        match self.persist_knowledge_artifact(
            memory_id,
            candidate,
            trigger_reason,
            &extracted.knowledge,
            existing_knowledge_sources,
        ) {
            Ok(outcome) => {
                result.apply(outcome.clone());
                self.finalize_artifact_audit(
                    audit_run_id,
                    candidate.timeline.id,
                    "knowledge",
                    &extracted.knowledge,
                    &outcome,
                )?;
                completed_artifacts += 1;
            }
            Err(error) => {
                tracing::warn!(
                    "bake artifact persist failed independently: timeline_id={} artifact=knowledge error={}",
                    candidate.timeline.id,
                    error,
                );
                if let Some(run_id) = audit_run_id {
                    self.storage.finalize_bake_artifact_audit(
                        run_id,
                        candidate.timeline.id,
                        "knowledge",
                        "failed",
                        Some(bake_retry_error_code(&error)),
                        None,
                    )?;
                }
                if first_error.is_none() {
                    first_error = Some(error);
                }
            }
        }

        match self
            .persist_document_artifact(
                memory_id,
                candidate,
                document_extraction,
                deterministic_document_recovery
                    .as_ref()
                    .map(|_| "candidate"),
                existing_document_sources,
                existing_document_urls,
            )
            .await
        {
            Ok(outcome) => {
                result.apply(outcome.clone());
                self.finalize_artifact_audit(
                    audit_run_id,
                    candidate.timeline.id,
                    "document",
                    document_extraction,
                    &outcome,
                )?;
                completed_artifacts += 1;
            }
            Err(error) => {
                tracing::warn!(
                    "bake artifact persist failed independently: timeline_id={} artifact=document error={}",
                    candidate.timeline.id,
                    error,
                );
                if let Some(run_id) = audit_run_id {
                    self.storage.finalize_bake_artifact_audit(
                        run_id,
                        candidate.timeline.id,
                        "document",
                        "failed",
                        Some(bake_retry_error_code(&error)),
                        None,
                    )?;
                }
                document_persist_error_code = Some(bake_retry_error_code(&error).to_string());
                if first_error.is_none() {
                    first_error = Some(error);
                }
            }
        }

        let deterministic_recovery_succeeded = deterministic_document_recovery.is_some()
            && document_persist_error_code.is_none()
            && self.document_already_persisted(candidate)?;
        if let (Some(run_id), true) = (audit_run_id, deterministic_recovery_succeeded) {
            let artifact_id = self.persisted_artifact_id("document", candidate.timeline.id)?;
            self.storage.finalize_bake_artifact_audit(
                run_id,
                candidate.timeline.id,
                "document",
                "recovered_from_source",
                extracted
                    .document
                    .reason
                    .as_deref()
                    .or(Some("deterministic_document_source_recovery")),
                artifact_id,
            )?;
            tracing::warn!(
                "bake document recovered from captured source: timeline_id={} artifact_id={:?} original_model_reason={:?}",
                candidate.timeline.id,
                artifact_id,
                extracted.document.reason,
            );
        }

        if audit_run_id.is_some()
            && !deterministic_recovery_succeeded
            && document_evidence(candidate).allows_auto_create
            && (!extracted.document.accepted || document_persist_error_code.is_some())
            && !self.document_already_persisted(candidate)?
        {
            if let Some(run_id) = audit_run_id {
                self.storage.finalize_bake_artifact_audit(
                    run_id,
                    candidate.timeline.id,
                    "document",
                    "false_negative",
                    document_persist_error_code
                        .as_deref()
                        .or(extracted.document.reason.as_deref())
                        .or(Some("deterministic_document_evidence_model_rejected")),
                    None,
                )?;
            }
            tracing::warn!(
                "bake document false-negative detected: timeline_id={} evidence={:?} model_reason={:?} persist_error_code={:?}",
                candidate.timeline.id,
                document_evidence(candidate).kind,
                extracted.document.reason,
                document_persist_error_code,
            );
            return Err(ApiError::Upstream {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                code: "BAKE_DOCUMENT_FALSE_NEGATIVE",
                message: "文档证据充分但模型未生成文档，已安排有界重试".to_string(),
            });
        }

        if completed_artifacts == 0 {
            return Err(first_error
                .unwrap_or_else(|| ApiError::Internal("bake 资产未产生可持久化结果".to_string())));
        }
        if first_error.is_some() {
            result.discarded_count += 1;
        }
        Ok(result)
    }

    fn persist_knowledge_artifact(
        &self,
        memory_id: Option<i64>,
        candidate: &BakeMemorySourceRecord,
        trigger_reason: &str,
        extraction: &BakeArtifactExtraction,
        existing_sources: &mut std::collections::HashSet<i64>,
    ) -> Result<CandidatePersistResult, ApiError> {
        let source_capture_ids = collect_source_capture_id_strings(&self.storage, candidate)?;
        let source_fingerprint = artifact_source_fingerprint(candidate);
        if let Some(existing_id) = source_fingerprint
            .as_deref()
            .map(|fingerprint| {
                self.storage
                    .find_bake_artifact_by_source_fingerprint("knowledge", fingerprint)
            })
            .transpose()?
            .flatten()
        {
            if let Some(existing) = self.storage.get_bake_knowledge(existing_id)? {
                self.merge_existing_knowledge_source_captures(&existing, &source_capture_ids)?;
                self.storage.record_bake_artifact_source(
                    "knowledge",
                    existing.id,
                    candidate.timeline.id,
                    source_fingerprint.as_deref(),
                )?;
                existing_sources.insert(candidate.timeline.id);
                tracing::info!(
                    "bake knowledge no_change: timeline_id={} knowledge_id={} reason=source_fingerprint_already_seen",
                    candidate.timeline.id,
                    existing.id,
                );
                return Ok(CandidatePersistResult::discarded());
            }
        }
        if existing_sources.contains(&candidate.timeline.id) {
            let mut existing = self
                .storage
                .find_bake_knowledge_by_timeline_id(candidate.timeline.id)?;
            if existing.is_none() {
                existing = self
                    .storage
                    .find_bake_artifact_by_source_timeline("knowledge", candidate.timeline.id)?
                    .map(|id| self.storage.get_bake_knowledge(id))
                    .transpose()?
                    .flatten();
            }
            let Some(existing) = existing else {
                tracing::warn!(
                    "bake knowledge existing set contained timeline_id={} but no row was found",
                    candidate.timeline.id,
                );
                existing_sources.remove(&candidate.timeline.id);
                return self.persist_knowledge_artifact(
                    memory_id,
                    candidate,
                    trigger_reason,
                    extraction,
                    existing_sources,
                );
            };
            if !extraction.accepted {
                self.merge_existing_knowledge_source_captures(&existing, &source_capture_ids)?;
                tracing::info!(
                    "bake knowledge source captures merged: timeline_id={} knowledge_id={} reason=already_has_knowledge_sidecar_rejected reason_text={:?}",
                    candidate.timeline.id,
                    existing.id,
                    extraction.reason,
                );
                return Ok(CandidatePersistResult::discarded());
            }

            let payload = extraction
                .payload
                .clone()
                .ok_or_else(|| ApiError::Upstream {
                    status: StatusCode::UNPROCESSABLE_ENTITY,
                    code: "BAKE_ARTIFACT_PAYLOAD_INVALID",
                    message: "bake knowledge 产物缺少必要 payload".to_string(),
                })?;
            let payload = parse_bake_knowledge_payload(payload, candidate)?;
            if let Some(memory_id) = memory_id {
                self.update_memory_match_metadata(
                    memory_id,
                    "knowledge",
                    payload.match_score,
                    payload.match_level.as_deref(),
                )?;
            }
            let decision = resolve_knowledge_decision(&payload);
            if decision.state != "published" {
                self.merge_existing_knowledge_source_captures(&existing, &source_capture_ids)?;
                return Ok(knowledge_gate_outcome(&payload, &decision));
            }
            let review_status = "auto_created".to_string();
            self.merge_existing_knowledge_artifact(
                &existing,
                candidate,
                trigger_reason,
                &payload,
                &review_status,
                &source_capture_ids,
            )?;
            tracing::info!(
                "bake knowledge merged: timeline_id={} knowledge_id={} sidecar_review_status={:?} match_score={:?} match_level={:?} resolved_review_status={}",
                candidate.timeline.id,
                existing.id,
                payload.review_status,
                payload.match_score,
                payload.match_level,
                review_status,
            );
            let mut outcome = CandidatePersistResult::default();
            outcome.knowledge_decision_state = Some("published");
            outcome.knowledge_quality_score = decision.score;
            outcome.knowledge_decision_reason_code = Some(decision.reason_code);
            outcome.knowledge_decision_reason_summary = Some(decision.reason_summary);
            return Ok(outcome);
        }
        if !extraction.accepted {
            tracing::info!(
                "bake knowledge discard: timeline_id={} reason=sidecar_rejected reason_text={:?}",
                candidate.timeline.id,
                extraction.reason,
            );
            return Ok(CandidatePersistResult::discarded());
        }

        let payload = extraction
            .payload
            .clone()
            .ok_or_else(|| ApiError::Upstream {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                code: "BAKE_ARTIFACT_PAYLOAD_INVALID",
                message: "bake knowledge 产物缺少必要 payload".to_string(),
            })?;
        let payload = parse_bake_knowledge_payload(payload, candidate)?;
        if let Some(memory_id) = memory_id {
            self.update_memory_match_metadata(
                memory_id,
                "knowledge",
                payload.match_score,
                payload.match_level.as_deref(),
            )?;
        }
        let decision = resolve_knowledge_decision(&payload);
        if decision.state != "published" {
            tracing::info!(
                "bake knowledge gated: timeline_id={} state={} score={:?} reason={}",
                candidate.timeline.id,
                decision.state,
                decision.score,
                decision.reason_code,
            );
            return Ok(knowledge_gate_outcome(&payload, &decision));
        }
        let review_status = "auto_created".to_string();
        tracing::info!(
            "bake knowledge accept: timeline_id={} sidecar_review_status={:?} match_score={:?} match_level={:?} resolved_review_status={}",
            candidate.timeline.id,
            payload.review_status,
            payload.match_score,
            payload.match_level,
            review_status,
        );
        let record = build_bake_knowledge_entry(
            candidate,
            &payload,
            &review_status,
            trigger_reason,
            &source_capture_ids,
        )?;
        let knowledge_id = self.storage.insert_bake_knowledge(&record)?;
        self.storage.record_bake_artifact_source(
            "knowledge",
            knowledge_id,
            candidate.timeline.id,
            source_fingerprint.as_deref(),
        )?;
        existing_sources.insert(candidate.timeline.id);
        let mut outcome = CandidatePersistResult::created_knowledge(true);
        outcome.knowledge_quality_score = decision.score;
        outcome.knowledge_decision_reason_code = Some(decision.reason_code);
        outcome.knowledge_decision_reason_summary = Some(decision.reason_summary);
        Ok(outcome)
    }

    fn merge_existing_knowledge_source_captures(
        &self,
        existing: &BakeKnowledgeRecord,
        source_capture_ids: &[String],
    ) -> Result<(), ApiError> {
        let merged_capture_ids = merge_string_lists(
            parse_optional_json_vec_string(&existing.source_capture_ids),
            source_capture_ids,
        );
        let source_capture_ids_json = to_json_string(&merged_capture_ids)?;
        self.storage.update_bake_knowledge_system(
            existing.id,
            &existing.title,
            &existing.summary,
            existing.content.as_deref(),
            existing.detailed_content.as_deref(),
            &existing.entities,
            existing.importance,
            Some(&source_capture_ids_json),
        )?;
        Ok(())
    }

    fn merge_existing_knowledge_artifact(
        &self,
        existing: &BakeKnowledgeRecord,
        candidate: &BakeMemorySourceRecord,
        trigger_reason: &str,
        payload: &BakeKnowledgeArtifactPayload,
        review_status: &str,
        source_capture_ids: &[String],
    ) -> Result<(), ApiError> {
        let merged_capture_ids = merge_string_lists(
            parse_optional_json_vec_string(&existing.source_capture_ids),
            source_capture_ids,
        );
        let merged_entities =
            merge_string_lists(parse_json_vec_string(&existing.entities), &payload.entities);
        let source_capture_ids_json = to_json_string(&merged_capture_ids)?;
        let entities_json = to_json_string(&merged_entities)?;
        let merged_details = merge_optional_text(
            existing.detailed_content.as_deref(),
            payload.details.as_deref(),
        );
        let mut next_content = parse_details(existing.content.as_deref());
        if !next_content.is_object() {
            next_content = json!({});
        }
        let next_details = next_content.as_object_mut().expect("object checked");
        next_details.insert(
            "source_timeline_id".to_string(),
            json!(candidate.timeline.id),
        );
        next_details.insert(
            "source_memory_ids".to_string(),
            json!([candidate.timeline.id.to_string()]),
        );
        next_details.insert("source_capture_ids".to_string(), json!(merged_capture_ids));
        next_details.insert(
            "source_timeline_ids".to_string(),
            json!([candidate.timeline.id.to_string()]),
        );
        next_details.insert(
            "match_score".to_string(),
            option_f64_json(payload.match_score),
        );
        next_details.insert(
            "match_level".to_string(),
            option_string_json(payload.match_level.as_deref()),
        );
        next_details.insert("creation_mode".to_string(), json!("llm_bake"));
        next_details.insert("review_status".to_string(), json!(review_status));
        next_details.insert(
            "evidence_summary".to_string(),
            option_string_json(payload.evidence_summary.as_deref()),
        );
        next_details.insert(
            "generation_version".to_string(),
            json!(BAKE_GENERATION_VERSION),
        );
        next_details.insert("trigger_reason".to_string(), json!(trigger_reason));
        next_details.insert("status".to_string(), json!(review_status));
        next_details.insert(
            "source_title".to_string(),
            json!(candidate.timeline.summary.clone()),
        );
        next_details.insert("merged_from_knowledge_id".to_string(), json!(existing.id));
        next_details.insert("merged_at_ms".to_string(), json!(now_ms()));

        let title = knowledge_title_from_payload(payload);
        let summary = knowledge_summary_from_payload(payload);
        let importance = payload
            .importance
            .unwrap_or(existing.importance)
            .max(existing.importance)
            .max(1);
        self.storage.update_bake_knowledge_system(
            existing.id,
            &title,
            &summary,
            Some(&next_content.to_string()),
            merged_details.as_deref(),
            &entities_json,
            importance,
            Some(&source_capture_ids_json),
        )?;
        Ok(())
    }

    async fn persist_document_artifact(
        &self,
        memory_id: Option<i64>,
        candidate: &BakeMemorySourceRecord,
        extraction: &BakeArtifactExtraction,
        forced_review_status: Option<&str>,
        existing_sources: &mut std::collections::HashSet<i64>,
        existing_urls: &mut std::collections::HashSet<String>,
    ) -> Result<CandidatePersistResult, ApiError> {
        let evidence = document_evidence(candidate);
        if !evidence.allows_auto_create {
            tracing::info!(
                "bake document discard: timeline_id={} reason=insufficient_document_evidence evidence_kind={:?} source_surface={:?} has_document_url={} has_document_page_title={} has_substantive_document_body={}",
                candidate.timeline.id,
                evidence.kind,
                evidence.source_surface,
                evidence.has_document_url,
                evidence.has_document_page_title,
                evidence.has_substantive_document_body,
            );
            return Ok(CandidatePersistResult::discarded());
        }

        if existing_sources.contains(&candidate.timeline.id) {
            if extraction.accepted {
                if let Some(existing_doc) = self
                    .storage
                    .find_bake_document_by_source_memory_id(candidate.timeline.id)?
                {
                    self.merge_document_with_sidecar(candidate, &existing_doc)
                        .await?;
                    tracing::info!(
                        "bake document merged: timeline_id={} doc_id={} reason=already_has_document_source",
                        candidate.timeline.id,
                        existing_doc.id,
                    );
                    return Ok(CandidatePersistResult::created_document(false));
                }
            }
            tracing::info!(
                "bake document discard: timeline_id={} reason=already_has_document",
                candidate.timeline.id,
            );
            return Ok(CandidatePersistResult::discarded());
        }
        let candidate_url_norm = candidate
            .capture_url
            .as_deref()
            .map(normalize_doc_url)
            .filter(|s| !s.is_empty());

        // URL 已存在：查询数据库中是否有该 URL 的文档，尝试合并而不是丢弃
        if let Some(ref u) = candidate_url_norm {
            if let Some(existing_doc) = self.storage.find_document_by_source_url(u)? {
                if extraction.accepted {
                    self.merge_document_with_sidecar(candidate, &existing_doc)
                        .await?;
                    existing_sources.insert(candidate.timeline.id);
                    existing_urls.insert(u.clone());
                    tracing::info!(
                        "bake document merged: timeline_id={} url={} doc_id={}",
                        candidate.timeline.id,
                        u,
                        existing_doc.id,
                    );
                    return Ok(CandidatePersistResult::created_document(false));
                } else {
                    tracing::info!(
                        "bake document discard: timeline_id={} reason=url_already_has_document_sidecar_rejected url={}",
                        candidate.timeline.id, u,
                    );
                    return Ok(CandidatePersistResult::discarded());
                }
            }
        }

        // URL 查询未命中时继续使用可靠来源标题兜底：
        // - 旧记录无 URL、后来 capture 补到 URL，仍应合入旧记录并补齐 URL；
        // - 双方都有 URL 且身份不同，则不能仅凭同名合并。
        if let Some(source_title) = document_source_title(candidate) {
            if let Some(existing_doc) = self
                .storage
                .find_bake_document_by_source_title(&source_title)?
            {
                if document_urls_compatible_for_title_match(
                    existing_doc.source_url.as_deref(),
                    candidate.capture_url.as_deref(),
                ) {
                    if extraction.accepted {
                        self.merge_document_with_sidecar(candidate, &existing_doc)
                            .await?;
                        existing_sources.insert(candidate.timeline.id);
                        tracing::info!(
                            "bake document merged: timeline_id={} source_title={} doc_id={} reason=same_document_source_title",
                            candidate.timeline.id,
                            source_title,
                            existing_doc.id,
                        );
                        return Ok(CandidatePersistResult::created_document(false));
                    }
                    tracing::info!(
                        "bake document discard: timeline_id={} reason=source_title_already_has_document_sidecar_rejected source_title={}",
                        candidate.timeline.id,
                        source_title,
                    );
                    return Ok(CandidatePersistResult::discarded());
                }
                tracing::info!(
                    "bake document title match ignored: timeline_id={} source_title={} existing_doc_id={} reason=different_document_url",
                    candidate.timeline.id,
                    source_title,
                    existing_doc.id,
                );
            }
        }

        if !extraction.accepted {
            tracing::info!(
                "bake document discard: timeline_id={} reason=sidecar_rejected reason_text={:?}",
                candidate.timeline.id,
                extraction.reason,
            );
            return Ok(CandidatePersistResult::discarded());
        }

        let payload = extraction
            .payload
            .clone()
            .ok_or_else(|| ApiError::Upstream {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                code: "BAKE_ARTIFACT_PAYLOAD_INVALID",
                message: "bake design 产物缺少必要 payload".to_string(),
            })?;
        let payload = parse_bake_document_payload(payload, candidate)?;
        if let Some(memory_id) = memory_id {
            self.update_memory_match_metadata(
                memory_id,
                "design",
                payload.match_score,
                payload.match_level.as_deref(),
            )?;
        }
        let review_status = forced_review_status
            .map(ToString::to_string)
            .unwrap_or_else(|| {
                resolve_review_status(
                    payload.review_status.as_deref(),
                    payload.match_score,
                    payload.match_level.as_deref(),
                )
            });
        tracing::info!(
            "bake document accept: timeline_id={} sidecar_review_status={:?} match_score={:?} match_level={:?} resolved_review_status={}",
            candidate.timeline.id,
            payload.review_status,
            payload.match_score,
            payload.match_level,
            review_status,
        );
        let source_capture_ids = collect_source_capture_id_strings(&self.storage, candidate)?;
        let linked_knowledge_ids = self
            .storage
            .find_bake_artifact_by_source_timeline("knowledge", candidate.timeline.id)?
            .map(|knowledge_id| vec![knowledge_id.to_string()])
            .unwrap_or_default();
        let document = build_bake_document(
            candidate,
            &payload,
            &review_status,
            &source_capture_ids,
            &linked_knowledge_ids,
        )?;
        let document_id = match self.storage.insert_bake_document(&document) {
            Ok(document_id) => document_id,
            Err(insert_error) => {
                // 数据库唯一 identity 是最终并发兜底。若另一个 run 刚插入同一文档，
                // 立即转为合并，不能把唯一键冲突暴露成整轮 bake 失败。
                let concurrently_created = candidate
                    .capture_url
                    .as_deref()
                    .map(|url| self.storage.find_document_by_source_url(url))
                    .transpose()?
                    .flatten();
                if let Some(existing_doc) = concurrently_created {
                    self.merge_document_with_sidecar(candidate, &existing_doc)
                        .await?;
                    existing_sources.insert(candidate.timeline.id);
                    tracing::info!(
                        "bake document merged after identity race: timeline_id={} doc_id={}",
                        candidate.timeline.id,
                        existing_doc.id,
                    );
                    return Ok(CandidatePersistResult::created_document(false));
                }
                return Err(insert_error.into());
            }
        };
        if let Some(fingerprint) = artifact_source_fingerprint(candidate) {
            self.storage.record_bake_document_source_fingerprint(
                document_id,
                &fingerprint,
                candidate.timeline.id,
            )?;
        }
        existing_sources.insert(candidate.timeline.id);
        if let Some(u) = candidate_url_norm {
            existing_urls.insert(u);
        }
        Ok(CandidatePersistResult::created_document(
            review_status == "auto_created",
        ))
    }

    async fn merge_document_with_sidecar(
        &self,
        candidate: &BakeMemorySourceRecord,
        existing_doc: &BakeDocumentRecord,
    ) -> Result<(), ApiError> {
        let source_fingerprint = artifact_source_fingerprint(candidate);
        if let Some(fingerprint) = source_fingerprint.as_deref() {
            if self
                .storage
                .has_bake_document_source_fingerprint(existing_doc.id, fingerprint)?
            {
                self.refresh_document_source_metadata(candidate, existing_doc)?;
                tracing::info!(
                    "bake document no_change: timeline_id={} doc_id={} reason=source_fingerprint_already_seen",
                    candidate.timeline.id,
                    existing_doc.id,
                );
                return Ok(());
            }
        }

        let (mut update, source_metadata_changed) =
            self.document_with_merged_source_metadata(candidate, existing_doc)?;
        let existing_json = serde_json::to_value(existing_doc)
            .map_err(|e| ApiError::Internal(format!("序列化已有文档失败: {e}")))?;
        let candidate_payload = map_extract_candidate_payload(candidate);
        let request_body = BakeMergeDocumentRequest {
            existing_document: existing_json,
            candidate: candidate_payload,
        };
        let url = format!("{}/bake/merge_document", self.sidecar_url);
        let response = self
            .client
            .post(&url)
            .json(&request_body)
            .timeout(Duration::from_secs(BAKE_SIDECAR_TIMEOUT_SECS))
            .send()
            .await
            .map_err(map_sidecar_request_error)?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            if source_metadata_changed {
                self.storage
                    .update_bake_document(existing_doc.id, &update)?;
            }
            // 来源元数据可以先补齐，但内容合并任务本身必须记录为永久失败，
            // 不能把 504 或其他 sidecar 错误伪装成成功后再次入队。
            let status = StatusCode::from_u16(status.as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
            let error = map_sidecar_error(status, body, "bake 文档合并服务");
            tracing::warn!(
                "bake merge_document sidecar error status={} code={}",
                status,
                error.code
            );
            return Err(ApiError::Upstream {
                status: error.status,
                code: error.code,
                message: error.message,
            });
        }

        let merged: BakeMergeDocumentResponse = response.json().await.map_err(|error| {
            tracing::warn!("解析 merge_document 成功响应失败: {}", error);
            ApiError::Upstream {
                status: StatusCode::BAD_GATEWAY,
                code: "BAKE_SIDECAR_RESPONSE_INVALID",
                message: "bake 文档合并服务返回了无法解析的响应".to_string(),
            }
        })?;

        if !merged.no_change {
            update.summary = merged.summary.or(update.summary);
            if let Some(merged_content) = merged.full_content {
                if document_merge_preserves_existing(
                    existing_doc.full_content.as_deref(),
                    &merged_content,
                ) {
                    update.full_content = Some(merged_content);
                } else {
                    tracing::warn!(
                        "bake document merge rejected because it would drop existing content: timeline_id={} doc_id={} existing_len={} merged_len={}",
                        candidate.timeline.id,
                        existing_doc.id,
                        existing_doc.full_content.as_deref().unwrap_or_default().chars().count(),
                        merged_content.chars().count(),
                    );
                }
            }
            update.evidence_summary = merged.evidence_summary.or(update.evidence_summary);
            update.match_score = merged.match_score.or(update.match_score);
            update.match_level = merged.match_level.or(update.match_level);
        } else {
            tracing::info!(
                "bake document no_change: timeline_id={} doc_id={} reason=content_already_covered",
                candidate.timeline.id,
                existing_doc.id,
            );
        }
        // 同一文档合并只更新正文和来源，不允许模型把稳定标题改成
        // “文档增量/新增内容”。历史上已出现这类标题时，用可靠来源标题修复。
        if generated_title_uses_incremental_wording(&update.title) {
            if let Some(source_title) = document_source_title(candidate) {
                update.title = source_title;
            }
        }

        // 更新 content_hash（若 full_content 有更新）
        if update.full_content.is_some() {
            update.content_hash = update.full_content.as_ref().map(|content| {
                let mut hasher = Sha256::new();
                hasher.update(content.as_bytes());
                format!("{:x}", hasher.finalize())
            });
        }

        self.storage
            .update_bake_document(existing_doc.id, &update)?;
        if let Some(fingerprint) = source_fingerprint {
            self.storage.record_bake_document_source_fingerprint(
                existing_doc.id,
                &fingerprint,
                candidate.timeline.id,
            )?;
        }
        Ok(())
    }

    fn refresh_document_source_metadata(
        &self,
        candidate: &BakeMemorySourceRecord,
        existing_doc: &BakeDocumentRecord,
    ) -> Result<bool, ApiError> {
        let (update, changed) =
            self.document_with_merged_source_metadata(candidate, existing_doc)?;
        if changed {
            self.storage
                .update_bake_document(existing_doc.id, &update)?;
        }
        Ok(changed)
    }

    /// URL 去重被跳过的候选仍要登记来源：文档已存在就立即把本候选的 timeline/capture
    /// 并进来源元数据；还没有文档（将由本轮先入队的同 URL 候选创建）则推入延迟列表，
    /// 待产物落盘后统一补登记。
    fn register_skipped_document_candidate_source(
        &self,
        candidate: &BakeMemorySourceRecord,
        document_url: &str,
        deferred_coalesce_sources: &mut Vec<BakeMemorySourceRecord>,
    ) -> Result<bool, ApiError> {
        if let Some(existing_doc) = self.storage.find_document_by_source_url(document_url)? {
            if self.refresh_document_source_metadata(candidate, &existing_doc)? {
                tracing::info!(
                    "bake coalesce: registered skipped candidate into existing document timeline_id={} doc_id={}",
                    candidate.timeline.id,
                    existing_doc.id,
                );
            }
            return Ok(true);
        }
        deferred_coalesce_sources.push(candidate.clone());
        Ok(false)
    }

    fn document_with_merged_source_metadata(
        &self,
        candidate: &BakeMemorySourceRecord,
        existing_doc: &BakeDocumentRecord,
    ) -> Result<(NewBakeDocument, bool), ApiError> {
        let mut changed = false;
        let mut source_ids = parse_json_vec_string(&existing_doc.source_memory_ids);
        let timeline_id = candidate.timeline.id.to_string();
        if !source_ids.contains(&timeline_id) {
            source_ids.push(timeline_id.clone());
            changed = true;
        }

        let mut source_capture_ids = parse_json_vec_string(&existing_doc.source_capture_ids);
        for capture_id in collect_source_capture_id_strings(&self.storage, candidate)? {
            if !source_capture_ids.contains(&capture_id) {
                source_capture_ids.push(capture_id);
                changed = true;
            }
        }

        let mut source_episode_ids = parse_json_vec_string(&existing_doc.source_episode_ids);
        for source_id in &source_ids {
            if !source_episode_ids.contains(source_id) {
                source_episode_ids.push(source_id.clone());
                changed = true;
            }
        }

        let mut linked_knowledge_ids = parse_json_vec_string(&existing_doc.linked_knowledge_ids);
        if let Some(knowledge_id) = self
            .storage
            .find_bake_artifact_by_source_timeline("knowledge", candidate.timeline.id)?
            .map(|value| value.to_string())
        {
            if !linked_knowledge_ids.contains(&knowledge_id) {
                linked_knowledge_ids.push(knowledge_id);
                changed = true;
            }
        }

        let mut update = bake_document_record_to_new(existing_doc.clone());
        update.source_memory_ids = to_json_string(&source_ids)?;
        update.source_capture_ids = to_json_string(&source_capture_ids)?;
        update.source_episode_ids = to_json_string(&source_episode_ids)?;
        update.linked_knowledge_ids = to_json_string(&linked_knowledge_ids)?;

        if normalize_optional_url(update.source_url.clone()).is_none() {
            if let Some(source_url) = normalize_optional_url(candidate.capture_url.clone()) {
                update.source_url = Some(source_url);
                changed = true;
            }
        }
        if update.source_app_name.is_none() && candidate.capture_app_name.is_some() {
            update.source_app_name = candidate.capture_app_name.clone();
            changed = true;
        }
        if let Some(source_title) = document_source_title(candidate) {
            // 真实来源标题晚于首帧到达时，纠正首帧留下的“知识库/未命名文档”。
            // 只替换确定性占位名，不覆盖已有的有效标题。
            if is_generic_document_source_title(&update.title) {
                update.title = source_title.clone();
                changed = true;
            }
            if update
                .source_win_title
                .as_deref()
                .is_none_or(is_generic_document_source_title)
            {
                if update.source_win_title.as_deref() != Some(source_title.as_str()) {
                    update.source_win_title = Some(source_title);
                    changed = true;
                }
            }
        }

        Ok((update, changed))
    }

    fn persist_sop_artifact(
        &self,
        memory_id: Option<i64>,
        candidate: &BakeMemorySourceRecord,
        trigger_reason: &str,
        extraction: &BakeArtifactExtraction,
        existing_sources: &mut std::collections::HashSet<i64>,
    ) -> Result<CandidatePersistResult, ApiError> {
        let source_capture_ids = collect_source_capture_id_strings(&self.storage, candidate)?;
        // 最终写入守卫与请求契约共用同一判定：直接交互通道要求“动作 + 可观察
        // 结果”，语义工作流通道要求执行面、执行语义和验证/完成语义同时成立。
        let eligibility = sop_eligibility(candidate);
        if !eligibility.eligible {
            tracing::info!(
                "bake sop discard: timeline_id={} reason={} effective_capture_count={}",
                candidate.timeline.id,
                eligibility.reason,
                eligibility.effective_capture_count,
            );
            return Ok(CandidatePersistResult::sop_outcome(
                "rejected",
                eligibility.reason,
            ));
        }
        let source_fingerprint = artifact_source_fingerprint(candidate);
        if let Some(existing_id) = source_fingerprint
            .as_deref()
            .map(|fingerprint| {
                self.storage
                    .find_bake_artifact_by_source_fingerprint("sop", fingerprint)
            })
            .transpose()?
            .flatten()
        {
            if let Some(existing) = self.storage.get_bake_sop(existing_id)? {
                self.merge_existing_sop_source_captures(&existing, &source_capture_ids)?;
                self.storage.record_bake_artifact_source(
                    "sop",
                    existing.id,
                    candidate.timeline.id,
                    source_fingerprint.as_deref(),
                )?;
                existing_sources.insert(candidate.timeline.id);
                tracing::info!(
                    "bake sop no_change: timeline_id={} sop_id={} reason=source_fingerprint_already_seen",
                    candidate.timeline.id,
                    existing.id,
                );
                return Ok(CandidatePersistResult::sop_outcome(
                    "reused",
                    "source_fingerprint_already_seen",
                ));
            }
        }
        if existing_sources.contains(&candidate.timeline.id) {
            if let Some(existing_id) = self
                .storage
                .find_bake_artifact_by_source_timeline("sop", candidate.timeline.id)?
            {
                if let Some(existing) = self.storage.get_bake_sop(existing_id)? {
                    self.merge_existing_sop_source_captures(&existing, &source_capture_ids)?;
                }
            }
            tracing::info!(
                "bake sop discard: timeline_id={} reason=already_has_sop",
                candidate.timeline.id,
            );
            return Ok(CandidatePersistResult::sop_outcome(
                "reused",
                "already_has_sop",
            ));
        }
        if !extraction.accepted {
            tracing::info!(
                "bake sop discard: timeline_id={} reason=sidecar_rejected reason_text={:?}",
                candidate.timeline.id,
                extraction.reason,
            );
            return Ok(CandidatePersistResult::sop_outcome(
                "rejected",
                extraction
                    .reason
                    .clone()
                    .unwrap_or_else(|| "sidecar_rejected".to_string()),
            ));
        }

        let payload = extraction
            .payload
            .clone()
            .ok_or_else(|| ApiError::Upstream {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                code: "BAKE_ARTIFACT_PAYLOAD_INVALID",
                message: "bake sop 产物缺少必要 payload".to_string(),
            })?;
        let payload = parse_bake_sop_payload(payload, candidate)?;
        if let Err(reason) = validate_bake_sop_evidence(&payload, candidate, &source_capture_ids) {
            tracing::info!(
                "bake sop discard: timeline_id={} reason={}",
                candidate.timeline.id,
                reason,
            );
            return Ok(CandidatePersistResult::sop_outcome("rejected", reason));
        }
        if let Some(memory_id) = memory_id {
            self.update_memory_match_metadata(
                memory_id,
                "sop",
                payload.match_score,
                payload.match_level.as_deref(),
            )?;
        }
        let review_status = resolve_review_status(
            payload.review_status.as_deref(),
            payload.match_score,
            payload.match_level.as_deref(),
        );
        tracing::info!(
            "bake sop accept: timeline_id={} sidecar_review_status={:?} match_score={:?} match_level={:?} resolved_review_status={}",
            candidate.timeline.id,
            payload.review_status,
            payload.match_score,
            payload.match_level,
            review_status,
        );
        let sop = build_bake_sop_entry(
            candidate,
            &payload,
            &review_status,
            trigger_reason,
            &source_capture_ids,
        )?;
        let sop_id = self.storage.insert_bake_sop(&sop)?;
        self.storage.record_bake_artifact_source(
            "sop",
            sop_id,
            candidate.timeline.id,
            source_fingerprint.as_deref(),
        )?;
        existing_sources.insert(candidate.timeline.id);
        Ok(CandidatePersistResult::created_sop(
            review_status == "auto_created",
        ))
    }

    fn merge_existing_sop_source_captures(
        &self,
        existing: &crate::storage::BakeSopRecord,
        source_capture_ids: &[String],
    ) -> Result<(), ApiError> {
        let merged_capture_ids = merge_string_lists(
            parse_optional_json_vec_string(&existing.source_capture_ids),
            source_capture_ids,
        );
        let source_capture_ids_json = to_json_string(&merged_capture_ids)?;
        self.storage
            .update_bake_sop_source_capture_ids(existing.id, &source_capture_ids_json)?;
        Ok(())
    }

    fn update_memory_match_metadata(
        &self,
        memory_id: i64,
        artifact_kind: &str,
        match_score: Option<f64>,
        match_level: Option<&str>,
    ) -> Result<(), ApiError> {
        let entry = self
            .storage
            .get_timeline_entry(memory_id)?
            .ok_or_else(|| ApiError::NotFound(format!("memory {memory_id} not found")))?;
        let details = parse_details(entry.details.as_deref())
            .as_object()
            .cloned()
            .unwrap_or_default();
        let mut next_details = serde_json::Map::from_iter(details);
        next_details.insert(
            format!("{artifact_kind}_match_score"),
            match_score.map_or(Value::Null, Value::from),
        );
        next_details.insert(
            format!("{artifact_kind}_match_level"),
            match_level.map_or(Value::Null, |value| Value::String(value.to_string())),
        );
        if artifact_kind == "design" {
            next_details.insert(
                "template_match_score".to_string(),
                match_score.map_or(Value::Null, Value::from),
            );
            next_details.insert(
                "template_match_level".to_string(),
                match_level.map_or(Value::Null, |value| Value::String(value.to_string())),
            );
        }
        self.storage.update_timeline_details_system(
            memory_id,
            &entry.summary,
            entry.overview.as_deref(),
            Some(&Value::Object(next_details).to_string()),
            &entry.entities,
        )?;
        Ok(())
    }

    pub fn get_overview(&self) -> Result<BakeOverviewPayload, ApiError> {
        let capture_count = self.storage.with_conn(|conn| {
            conn.query_row("SELECT COUNT(*) FROM captures", [], |row| row.get(0))
                .map_err(StorageError::Sqlite)
        })?;
        let memory_entries = self.storage.list_timelines_paginated(None, 5000, 0)?;
        let knowledge_entries = self
            .storage
            .list_bake_knowledge_paginated(None, 5000, 0)?
            .into_iter()
            .filter(is_current_bake_entry)
            .collect::<Vec<_>>();
        let templates = self
            .storage
            .list_bake_documents()?
            .into_iter()
            .filter(is_current_bake_document)
            .collect::<Vec<_>>();
        let sop_entries = self
            .storage
            .list_timelines_by_category(CATEGORY_BAKE_SOP)?
            .into_iter()
            .filter(is_current_bake_entry)
            .collect::<Vec<_>>();
        let latest_run = self.storage.get_latest_bake_run()?;
        let memory_count = self.storage.count_timelines(None)?;
        let data_sources = self.storage.list_data_sources(None, 5000, 0)?.0;
        let data_count = data_sources.len() as i64;
        let inventory_trend = build_inventory_trend(
            &memory_entries
                .iter()
                .map(|record| record.created_at_ms)
                .collect::<Vec<_>>(),
            &data_sources
                .iter()
                .map(|record| record.created_at)
                .collect::<Vec<_>>(),
            &knowledge_entries
                .iter()
                .map(|record| record.created_at_ms)
                .collect::<Vec<_>>(),
            &templates
                .iter()
                .map(|record| record.created_at)
                .collect::<Vec<_>>(),
            &sop_entries
                .iter()
                .map(|record| record.created_at_ms)
                .collect::<Vec<_>>(),
        );

        let pending_candidates = 0;

        let mut recent_activities: Vec<BakeActivityRecord> = memory_entries
            .iter()
            .take(3)
            .map(|entry| BakeActivityRecord {
                message: format!("情节记忆《{}》已进入烤面包队列", entry.summary),
                ts: entry.updated_at_ms,
            })
            .collect();
        recent_activities.extend(knowledge_entries.iter().take(2).map(|entry| {
            BakeActivityRecord {
                message: format!("知识《{}》已由 LLM 烤面包提炼", entry.summary),
                ts: entry.updated_at_ms,
            }
        }));
        recent_activities.extend(templates.iter().take(2).map(|template| BakeActivityRecord {
            message: format!("模板《{}》状态已更新为 {}", template.title, template.status),
            ts: template.updated_at,
        }));
        if let Some(run) = latest_run.as_ref() {
            recent_activities.push(BakeActivityRecord {
                message: format_bake_run_activity(run),
                ts: run.completed_at.unwrap_or(run.started_at),
            });
        }
        recent_activities.sort_by(|a, b| b.ts.cmp(&a.ts));

        let overview = BakeOverviewRecord {
            capture_count,
            memory_count,
            data_count,
            knowledge_count: knowledge_entries.len() as i64,
            template_count: templates.len() as i64,
            sop_count: sop_entries.len() as i64,
            pending_candidates,
            auto_created_today: latest_run
                .as_ref()
                .map(|run| run.auto_created_count)
                .unwrap_or(0),
            candidate_today: latest_run
                .as_ref()
                .map(|run| run.candidate_count)
                .unwrap_or(0),
            discarded_today: latest_run
                .as_ref()
                .map(|run| run.discarded_count)
                .unwrap_or(0),
            last_bake_run_status: latest_run.as_ref().map(|run| run.status.clone()),
            last_bake_run_at: latest_run
                .as_ref()
                .map(|run| run.completed_at.unwrap_or(run.started_at)),
            last_trigger_reason: latest_run.as_ref().map(|run| run.trigger_reason.clone()),
            knowledge_auto_count: latest_run
                .as_ref()
                .map(|run| run.knowledge_created_count)
                .unwrap_or(0),
            template_auto_count: latest_run
                .as_ref()
                .map(|run| run.document_created_count)
                .unwrap_or(0),
            sop_auto_count: latest_run
                .as_ref()
                .map(|run| run.sop_created_count)
                .unwrap_or(0),
            recent_activities,
        };

        Ok(BakeOverviewPayload {
            capture_count: overview.capture_count,
            memory_count: overview.memory_count,
            data_count: overview.data_count,
            knowledge_count: overview.knowledge_count,
            template_count: overview.template_count,
            sop_count: overview.sop_count,
            pending_candidates: overview.pending_candidates,
            auto_created_today: overview.auto_created_today,
            candidate_today: overview.candidate_today,
            discarded_today: overview.discarded_today,
            last_bake_run_status: overview.last_bake_run_status,
            last_bake_run_at: overview.last_bake_run_at,
            last_trigger_reason: overview.last_trigger_reason,
            knowledge_auto_count: overview.knowledge_auto_count,
            template_auto_count: overview.template_auto_count,
            sop_auto_count: overview.sop_auto_count,
            recent_activities: overview
                .recent_activities
                .into_iter()
                .map(|item| item.message)
                .collect(),
            inventory_trend,
        })
    }

    fn update_memory_status(&self, id: i64, status: &str) -> Result<BakeMemoryPayload, ApiError> {
        let entry = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("memory {id} not found")))?;
        let details = parse_details(entry.details.as_deref())
            .as_object()
            .cloned()
            .unwrap_or_default();
        let mut next_details = serde_json::Map::from_iter(details);
        next_details.insert("status".to_string(), json!(status));
        self.storage.update_timeline_details_system(
            id,
            &entry.summary,
            entry.overview.as_deref(),
            Some(&Value::Object(next_details).to_string()),
            &entry.entities,
        )?;
        let updated = self
            .storage
            .get_timeline_entry(id)?
            .ok_or_else(|| ApiError::NotFound(format!("memory {id} not found after update")))?;
        self.map_memory_record_with_capture_url(updated)
    }

    fn map_memory_record_with_capture_url(
        &self,
        record: TimelineRecord,
    ) -> Result<BakeMemoryPayload, ApiError> {
        let capture_url = self
            .storage
            .get_capture(record.capture_id)?
            .and_then(|capture| normalize_optional_url(capture.url));
        Ok(map_memory_record(record, capture_url))
    }

    fn map_knowledge_record_with_capture_url(
        &self,
        record: TimelineRecord,
    ) -> Result<BakeKnowledgePayload, ApiError> {
        let source_url = if record.capture_id > 0 {
            self.storage
                .get_capture(record.capture_id)?
                .and_then(|capture| normalize_optional_url(capture.url))
        } else {
            None
        };
        let mut payload = map_bake_knowledge_record(record);
        payload.source_url = source_url;
        Ok(payload)
    }

    /// 指纹预筛：候选源文本指纹已命中所有可能产出的产物类型时返回 true。
    ///
    /// knowledge / sop 需已有同指纹产物；document 仅在候选满足自动建档证据
    /// 时才要求同指纹文档存在。查询失败时保守返回 false，退回 LLM 提炼。
    fn candidate_fully_covered_by_fingerprints(&self, candidate: &BakeMemorySourceRecord) -> bool {
        let fingerprint = match artifact_source_fingerprint(candidate) {
            Some(value) => value,
            None => return false,
        };
        let knowledge_hit = self
            .storage
            .find_bake_artifact_by_source_fingerprint("knowledge", &fingerprint)
            .map(|v| v.is_some())
            .unwrap_or(false);
        if !knowledge_hit {
            return false;
        }
        let sop_hit = self
            .storage
            .find_bake_artifact_by_source_fingerprint("sop", &fingerprint)
            .map(|v| v.is_some())
            .unwrap_or(false);
        if !sop_hit {
            return false;
        }
        if !document_evidence(candidate).allows_auto_create {
            return true;
        }
        self.storage
            .find_bake_document_id_by_source_fingerprint(&fingerprint)
            .map(|v| v.is_some())
            .unwrap_or(false)
    }
}

#[derive(Debug, Clone, Default)]
struct CandidatePersistResult {
    auto_created_count: i64,
    candidate_count: i64,
    discarded_count: i64,
    knowledge_created_count: i64,
    document_created_count: i64,
    sop_created_count: i64,
    sop_persist_status: Option<&'static str>,
    sop_persist_reason: Option<String>,
    knowledge_decision_state: Option<&'static str>,
    knowledge_quality_score: Option<f64>,
    knowledge_decision_reason_code: Option<&'static str>,
    knowledge_decision_reason_summary: Option<String>,
    knowledge_shadow_payload_json: Option<String>,
}

impl CandidatePersistResult {
    fn discarded() -> Self {
        Self {
            discarded_count: 1,
            ..Self::default()
        }
    }

    fn created_knowledge(auto_created: bool) -> Self {
        Self {
            auto_created_count: if auto_created { 1 } else { 0 },
            candidate_count: if auto_created { 0 } else { 1 },
            knowledge_created_count: 1,
            knowledge_decision_state: Some("published"),
            knowledge_decision_reason_code: Some("publish_threshold_met"),
            ..Self::default()
        }
    }

    fn knowledge_gated(
        state: &'static str,
        score: Option<f64>,
        reason_code: &'static str,
        reason_summary: String,
        shadow_payload_json: Option<String>,
    ) -> Self {
        Self {
            candidate_count: if state == "shadow" { 1 } else { 0 },
            discarded_count: if state == "timeline_only" { 1 } else { 0 },
            knowledge_decision_state: Some(state),
            knowledge_quality_score: score,
            knowledge_decision_reason_code: Some(reason_code),
            knowledge_decision_reason_summary: Some(reason_summary),
            knowledge_shadow_payload_json: shadow_payload_json,
            ..Self::default()
        }
    }

    fn created_document(auto_created: bool) -> Self {
        Self {
            auto_created_count: if auto_created { 1 } else { 0 },
            candidate_count: if auto_created { 0 } else { 1 },
            document_created_count: 1,
            ..Self::default()
        }
    }

    fn created_sop(auto_created: bool) -> Self {
        Self {
            auto_created_count: if auto_created { 1 } else { 0 },
            candidate_count: if auto_created { 0 } else { 1 },
            sop_created_count: 1,
            sop_persist_status: Some("created"),
            sop_persist_reason: Some("created".to_string()),
            ..Self::default()
        }
    }

    fn sop_outcome(status: &'static str, reason: impl Into<String>) -> Self {
        Self {
            discarded_count: if status == "created" { 0 } else { 1 },
            sop_persist_status: Some(status),
            sop_persist_reason: Some(reason.into()),
            ..Self::default()
        }
    }

    fn apply(&mut self, other: Self) {
        self.auto_created_count += other.auto_created_count;
        self.candidate_count += other.candidate_count;
        self.discarded_count += other.discarded_count;
        self.knowledge_created_count += other.knowledge_created_count;
        self.document_created_count += other.document_created_count;
        self.sop_created_count += other.sop_created_count;
        if other.sop_persist_status.is_some() {
            self.sop_persist_status = other.sop_persist_status;
            self.sop_persist_reason = other.sop_persist_reason;
        }
        if other.knowledge_decision_state.is_some() {
            self.knowledge_decision_state = other.knowledge_decision_state;
            self.knowledge_quality_score = other.knowledge_quality_score;
            self.knowledge_decision_reason_code = other.knowledge_decision_reason_code;
            self.knowledge_decision_reason_summary = other.knowledge_decision_reason_summary;
            self.knowledge_shadow_payload_json = other.knowledge_shadow_payload_json;
        }
    }
}

fn map_extract_candidate_payload(
    candidate: &BakeMemorySourceRecord,
) -> BakeExtractCandidatePayload {
    let source_capture_count = source_capture_id_strings(candidate).len() as i64;
    let sop_eligibility = sop_eligibility(candidate);
    BakeExtractCandidatePayload {
        source_timeline_id: candidate.timeline.id,
        source_capture_id: candidate.timeline.capture_id,
        source_capture_count,
        effective_capture_count: sop_eligibility.effective_capture_count,
        sop_evidence_mode: sop_eligibility.mode.map(|mode| mode.as_str().to_string()),
        timeline_category: candidate.timeline.category.clone(),
        summary: candidate.timeline.summary.clone(),
        overview: candidate.timeline.overview.clone(),
        details: candidate.timeline.details.clone(),
        work_item: candidate.work_item.clone(),
        work_status: candidate.work_status.clone(),
        work_progress: candidate.work_progress.clone(),
        entities: parse_json_vec_string(&candidate.timeline.entities),
        importance: candidate.timeline.importance,
        occurrence_count: candidate.timeline.occurrence_count,
        observed_at: candidate.timeline.observed_at,
        event_time_start: candidate.timeline.event_time_start,
        event_time_end: candidate.timeline.event_time_end,
        start_time: candidate.timeline.start_time,
        end_time: candidate.timeline.end_time,
        duration_minutes: candidate.timeline.duration_minutes,
        time_range_start: candidate.timeline.time_range_start,
        time_range_end: candidate.timeline.time_range_end,
        key_timestamps: candidate
            .timeline
            .key_timestamps
            .as_deref()
            .and_then(|raw| serde_json::from_str::<Value>(raw).ok()),
        history_view: candidate.timeline.history_view,
        content_origin: candidate.timeline.content_origin.clone(),
        activity_type: candidate.timeline.activity_type.clone(),
        evidence_strength: candidate.timeline.evidence_strength.clone(),
        capture_ts: candidate.capture_ts,
        capture_app_name: candidate.capture_app_name.clone(),
        capture_win_title: candidate.capture_win_title.clone(),
        capture_ax_text: candidate.capture_ax_text.clone(),
        capture_ocr_text: candidate.capture_ocr_text.clone(),
        capture_input_text: candidate.capture_input_text.clone(),
        capture_audio_text: candidate.capture_audio_text.clone(),
        capture_url: candidate.capture_url.clone(),
        // sidecar 也消费成员帧选出的可靠标题，避免模型继续被主帧占位名误导。
        capture_webpage_title: document_source_title(candidate)
            .or_else(|| candidate.capture_webpage_title.clone()),
        url_aggregated_text: candidate.url_aggregated_text.clone(),
        url_aggregated_capture_count: candidate.url_aggregated_capture_count,
        action_trace: candidate.action_trace.clone(),
        document_evidence: document_evidence(candidate),
    }
}

fn new_bake_candidate_audit(
    run_id: i64,
    candidate: &BakeMemorySourceRecord,
    persist_status: &str,
    persist_reason: Option<&str>,
) -> NewBakeCandidateAudit {
    let source_capture_count = source_capture_id_strings(candidate).len() as i64;
    let eligibility = sop_eligibility(candidate);
    let (sop_eligible, sop_eligibility_reason) = if persist_status != "queued" {
        (false, persist_reason.unwrap_or("precheck_skipped"))
    } else {
        (eligibility.eligible, eligibility.reason)
    };
    NewBakeCandidateAudit {
        run_id,
        timeline_id: candidate.timeline.id,
        lane: if candidate.retry_failure_count > 0 {
            "retry".to_string()
        } else {
            "fresh".to_string()
        },
        source_capture_count,
        effective_capture_count: eligibility.effective_capture_count,
        sop_eligible,
        sop_eligibility_reason: Some(sop_eligibility_reason.to_string()),
        sop_evidence_mode: eligibility.mode.map(|mode| mode.as_str().to_string()),
        persist_status: persist_status.to_string(),
        persist_reason: persist_reason.map(ToString::to_string),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum SopEvidenceMode {
    DirectInteraction,
    SemanticWorkflow,
}

impl SopEvidenceMode {
    fn as_str(self) -> &'static str {
        match self {
            Self::DirectInteraction => "direct_interaction",
            Self::SemanticWorkflow => "semantic_workflow",
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct SopEligibility {
    eligible: bool,
    reason: &'static str,
    mode: Option<SopEvidenceMode>,
    effective_capture_count: i64,
}

fn sop_eligibility(candidate: &BakeMemorySourceRecord) -> SopEligibility {
    let source_capture_count = source_capture_id_strings(candidate).len() as i64;
    if source_capture_count < 2 {
        return SopEligibility {
            eligible: false,
            reason: "insufficient_source_capture_count",
            mode: None,
            effective_capture_count: source_capture_count,
        };
    }

    let evidence_ids = candidate
        .action_trace
        .iter()
        .filter(|record| matches!(record.evidence_role.as_deref(), Some("action" | "result")))
        .map(|record| record.capture_id)
        .collect::<HashSet<_>>();
    let action_records = candidate
        .action_trace
        .iter()
        .filter(|record| record.evidence_role.as_deref() == Some("action"))
        .collect::<Vec<_>>();
    let result_records = candidate
        .action_trace
        .iter()
        .filter(|record| record.evidence_role.as_deref() == Some("result"))
        .collect::<Vec<_>>();
    let has_attributed_result = result_records.iter().any(|result| {
        action_records.iter().any(|action| {
            result.ts >= action.ts
                && result.ts.saturating_sub(action.ts) <= 120_000
                && result.capture_id != action.capture_id
        })
    });
    if evidence_ids.len() >= 2 && !action_records.is_empty() && has_attributed_result {
        return SopEligibility {
            eligible: true,
            reason: "eligible_direct_action_result",
            mode: Some(SopEvidenceMode::DirectInteraction),
            effective_capture_count: evidence_ids.len() as i64,
        };
    }

    let activity_type = candidate
        .timeline
        .activity_type
        .as_deref()
        .unwrap_or_default()
        .to_ascii_lowercase();
    let category = candidate.timeline.category.to_ascii_lowercase();
    let execution_surface = activity_type
        .split(|ch: char| !ch.is_ascii_alphanumeric() && ch != '_')
        .any(|value| matches!(value, "coding" | "ask_ai" | "debugging" | "deployment"))
        || category.contains("代码")
        || matches!(category.as_str(), "coding" | "debugging" | "deployment");
    let work_status = candidate.work_status.as_deref().unwrap_or_default().trim();
    let semantic_text = [
        Some(candidate.timeline.summary.as_str()),
        candidate.timeline.overview.as_deref(),
        candidate.timeline.details.as_deref(),
        candidate.work_item.as_deref(),
        candidate.work_progress.as_deref(),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join("\n")
    .to_ascii_lowercase();
    let execution_markers = [
        "修复",
        "修改",
        "实现",
        "编写",
        "新增",
        "删除",
        "调整",
        "执行",
        "运行",
        "完成",
        "fixed",
        "implemented",
        "patched",
        "created",
        "updated",
        "ran ",
    ];
    let verification_markers = [
        "测试通过",
        "验证通过",
        "编译通过",
        "构建成功",
        "检查通过",
        "已上线",
        "发布成功",
        "问题解决",
        "通过",
        "成功",
        "tests passed",
        "test passed",
        "build succeeded",
        "verified",
        "resolved",
        "completed",
    ];
    let has_execution = execution_markers
        .iter()
        .any(|marker| semantic_text.contains(marker));
    let has_verification = verification_markers
        .iter()
        .any(|marker| semantic_text.contains(marker));
    let completed_work = work_status == "completed";
    let plan_only = matches!(work_status, "pending" | "blocked")
        || (["计划", "准备", "待处理", "todo", "will "]
            .iter()
            .any(|marker| semantic_text.contains(marker))
            && !has_verification
            && !completed_work);
    if execution_surface
        && has_execution
        && (has_verification || completed_work)
        && !plan_only
        && !action_records.is_empty()
        && has_attributed_result
    {
        return SopEligibility {
            eligible: true,
            reason: "eligible_semantic_workflow_with_action_anchor",
            mode: Some(SopEvidenceMode::SemanticWorkflow),
            effective_capture_count: evidence_ids.len() as i64,
        };
    }

    let passive_report_only = candidate.action_trace.iter().all(|record| {
        record.evidence_role.as_deref() != Some("action")
            && matches!(
                record.event_type.as_str(),
                "app_switch" | "browser_navigation" | "auto" | "key_pause" | "scroll"
            )
            && record
                .input_text
                .as_deref()
                .map_or(true, |value| value.trim().is_empty())
    });
    let reason = if passive_report_only {
        "passive_report_only"
    } else if action_records.is_empty() {
        "missing_real_action"
    } else if !has_attributed_result {
        "missing_attributed_result"
    } else {
        "insufficient_sop_evidence"
    };

    SopEligibility {
        eligible: false,
        reason,
        mode: None,
        effective_capture_count: evidence_ids.len() as i64,
    }
}

fn map_sidecar_request_error(err: reqwest::Error) -> ApiError {
    let msg = err.to_string();
    if err.is_timeout() || msg.contains("timed out") || msg.contains("timeout") {
        tracing::warn!("bake sidecar 响应超时: {}", err);
        ApiError::Upstream {
            status: StatusCode::GATEWAY_TIMEOUT,
            code: "INFERENCE_TIMEOUT",
            message: format!(
                "bake 提炼请求超时（>{} 秒），请稍后重试",
                BAKE_SIDECAR_TIMEOUT_SECS
            ),
        }
    } else {
        tracing::warn!("无法连接到 bake sidecar: {}", err);
        ApiError::Upstream {
            status: StatusCode::SERVICE_UNAVAILABLE,
            code: "SIDECAR_UNAVAILABLE",
            message: format!("bake 提炼服务不可用，请确认 AI Sidecar 已正常启动: {err}"),
        }
    }
}

fn is_untracked_transient_bake_error(error: &ApiError) -> bool {
    matches!(
        error,
        ApiError::Upstream { code, .. }
            if matches!(
                *code,
                "INFERENCE_PREEMPTED"
                    | "MODEL_RATE_LIMITED"
                    | "MODEL_UNAVAILABLE"
                    | "SIDECAR_UNAVAILABLE"
            )
    )
}

fn bake_retry_failure_summary(error: &ApiError) -> String {
    match error {
        ApiError::Upstream { status, code, .. } => {
            format!("bake_error code={code} status={}", status.as_u16())
        }
        _ => error.to_string(),
    }
}

fn bake_retry_error_code(error: &ApiError) -> &str {
    match error {
        ApiError::Upstream { code, .. } => code,
        _ => "BAKE_INTERNAL_ERROR",
    }
}

fn is_retryable_bake_candidate_error(error: &ApiError) -> bool {
    matches!(
        error,
        ApiError::Upstream { code, .. }
            if matches!(
                *code,
                "INFERENCE_TIMEOUT"
                    | "GATEWAY_TIMEOUT"
                    | "BAKE_OUTPUT_TRUNCATED"
                    | "BAKE_OUTPUT_INVALID"
                    | "BAKE_MODEL_RESPONSE_INVALID"
                    | "BAKE_MODEL_UPSTREAM_ERROR"
                    | "BAKE_SIDECAR_RESPONSE_INVALID"
                    | "BAKE_ARTIFACT_PAYLOAD_INVALID"
                    | "BAKE_DOCUMENT_FALSE_NEGATIVE"
                    | "BAKE_INTERNAL_ERROR"
                    | "BAKE_UNCLASSIFIED_UPSTREAM_ERROR"
            )
    )
}

fn is_transient_bake_error(error: &ApiError) -> bool {
    is_untracked_transient_bake_error(error) || is_retryable_bake_candidate_error(error)
}

fn is_bake_candidate_timeout(error: &ApiError) -> bool {
    matches!(
        error,
        ApiError::Upstream {
            status: StatusCode::GATEWAY_TIMEOUT,
            ..
        }
    )
}

fn merge_bake_candidate_lanes(
    fresh: Vec<BakeMemorySourceRecord>,
    retry: Vec<BakeMemorySourceRecord>,
    limit: usize,
    run_id: i64,
) -> Vec<BakeMemorySourceRecord> {
    let mut fresh = std::collections::VecDeque::from(fresh);
    let mut retry = std::collections::VecDeque::from(retry);
    let mut merged = Vec::with_capacity(limit.min(fresh.len().saturating_add(retry.len())));

    for slot in 0..limit {
        let prefer_retry = (run_id.saturating_add(slot as i64)).rem_euclid(5) == 0;
        let candidate = if prefer_retry {
            retry.pop_front().or_else(|| fresh.pop_front())
        } else {
            fresh.pop_front().or_else(|| retry.pop_front())
        };
        if let Some(candidate) = candidate {
            merged.push(candidate);
        } else {
            break;
        }
    }
    merged
}

fn parse_bake_knowledge_payload(
    value: Value,
    candidate: &BakeMemorySourceRecord,
) -> Result<BakeKnowledgeArtifactPayload, ApiError> {
    let mut payload: BakeKnowledgeArtifactPayload =
        serde_json::from_value(value).map_err(|err| {
            tracing::warn!("解析 bake knowledge payload 失败: {}", err);
            ApiError::Upstream {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                code: "BAKE_ARTIFACT_PAYLOAD_INVALID",
                message: "bake knowledge 产物结构无效".to_string(),
            }
        })?;
    if payload.summary.trim().is_empty() {
        payload.summary = candidate.timeline.summary.clone();
    }
    Ok(payload)
}

fn parse_bake_document_payload(
    value: Value,
    candidate: &BakeMemorySourceRecord,
) -> Result<BakeDocumentArtifactPayload, ApiError> {
    let mut payload: BakeDocumentArtifactPayload =
        serde_json::from_value(value).map_err(|err| {
            tracing::warn!("解析 bake design payload 失败: {}", err);
            ApiError::Upstream {
                status: StatusCode::UNPROCESSABLE_ENTITY,
                code: "BAKE_ARTIFACT_PAYLOAD_INVALID",
                message: "bake design 产物结构无效".to_string(),
            }
        })?;
    if let Some(source_title) = document_source_title(candidate) {
        payload.title = source_title;
    } else if payload.title.trim().is_empty() {
        payload.title = candidate
            .capture_webpage_title
            .as_deref()
            .or(candidate.capture_win_title.as_deref())
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or(candidate.timeline.summary.as_str())
            .to_string();
    } else {
        payload.title = sanitize_generated_document_title(&payload.title);
    }
    Ok(payload)
}

fn parse_bake_sop_payload(
    value: Value,
    candidate: &BakeMemorySourceRecord,
) -> Result<BakeSopArtifactPayload, ApiError> {
    let mut payload: BakeSopArtifactPayload = serde_json::from_value(value).map_err(|err| {
        tracing::warn!("解析 bake sop payload 失败: {}", err);
        ApiError::Upstream {
            status: StatusCode::UNPROCESSABLE_ENTITY,
            code: "BAKE_ARTIFACT_PAYLOAD_INVALID",
            message: "bake sop 产物结构无效".to_string(),
        }
    })?;
    if payload.summary.trim().is_empty() {
        payload.summary = candidate.timeline.summary.clone();
    }
    Ok(payload)
}

fn validate_bake_sop_evidence(
    payload: &BakeSopArtifactPayload,
    candidate: &BakeMemorySourceRecord,
    source_capture_ids: &[String],
) -> Result<(), &'static str> {
    if !(3..=8).contains(&payload.steps.len())
        || payload.steps.iter().any(|step| step.trim().is_empty())
    {
        return Err("insufficient_sop_steps");
    }
    let eligibility = sop_eligibility(candidate);
    let evidence_mode = eligibility.mode.ok_or(eligibility.reason)?;

    let source_capture_id_set = source_capture_ids.iter().cloned().collect::<HashSet<_>>();
    let evidence_timestamps = candidate
        .action_trace
        .iter()
        .filter(|record| matches!(record.evidence_role.as_deref(), Some("action" | "result")))
        .map(|record| (record.capture_id.to_string(), record.ts))
        .collect::<HashMap<_, _>>();
    let mut evidence_by_step = HashMap::<i64, &BakeSopStepEvidencePayload>::new();
    for evidence in &payload.step_evidence {
        if evidence.step_index < 1
            || evidence.step_index > payload.steps.len() as i64
            || evidence_by_step
                .insert(evidence.step_index, evidence)
                .is_some()
        {
            return Err("invalid_sop_step_evidence");
        }
    }
    if evidence_by_step.len() != payload.steps.len() {
        return Err("missing_sop_step_evidence");
    }

    let mut distinct_evidence = HashSet::new();
    let mut previous_step_ts: Option<i64> = None;
    for step_index in 1..=payload.steps.len() as i64 {
        let evidence = evidence_by_step
            .get(&step_index)
            .ok_or("missing_sop_step_evidence")?;
        if evidence.capture_ids.is_empty() {
            return Err("missing_sop_step_evidence");
        }
        let mut step_ts: Option<i64> = None;
        for capture_id in &evidence.capture_ids {
            if !source_capture_id_set.contains(capture_id) {
                return Err("invalid_sop_step_evidence");
            }
            let ts = evidence_timestamps
                .get(capture_id)
                .copied()
                .ok_or(match evidence_mode {
                    SopEvidenceMode::DirectInteraction => "non_operation_sop_step_evidence",
                    SopEvidenceMode::SemanticWorkflow => "invalid_sop_step_evidence",
                })?;
            distinct_evidence.insert(capture_id.clone());
            step_ts = Some(step_ts.map_or(ts, |current| current.min(ts)));
        }
        let step_ts = step_ts.ok_or("missing_sop_step_evidence")?;
        if previous_step_ts.is_some_and(|previous| step_ts < previous) {
            return Err("non_chronological_sop_step_evidence");
        }
        previous_step_ts = Some(step_ts);
    }
    if distinct_evidence.len() < 2 {
        return Err("insufficient_distinct_sop_evidence");
    }
    Ok(())
}

fn document_merge_preserves_existing(existing: Option<&str>, merged: &str) -> bool {
    let existing = existing.unwrap_or_default().trim();
    existing.is_empty() || merged.contains(existing)
}

fn map_sidecar_error(
    status: StatusCode,
    body_text: String,
    service_name: &str,
) -> BakeSidecarError {
    #[derive(Deserialize)]
    struct ErrorEnvelope {
        code: Option<String>,
        retryable: Option<bool>,
        scope: Option<String>,
    }

    fn has_valid_contract(envelope: &ErrorEnvelope) -> bool {
        let Some(code) = envelope.code.as_deref() else {
            return false;
        };
        match code {
            "INFERENCE_PREEMPTED"
            | "MODEL_RATE_LIMITED"
            | "MODEL_UNAVAILABLE"
            | "SIDECAR_UNAVAILABLE" => {
                envelope.scope.as_deref() == Some("service") && envelope.retryable == Some(true)
            }
            "INFERENCE_TIMEOUT"
            | "BAKE_OUTPUT_TRUNCATED"
            | "BAKE_OUTPUT_INVALID"
            | "BAKE_MODEL_RESPONSE_INVALID"
            | "BAKE_MODEL_UPSTREAM_ERROR"
            | "BAKE_INTERNAL_ERROR" => {
                envelope.scope.as_deref() == Some("candidate") && envelope.retryable == Some(true)
            }
            "BAKE_MODEL_REQUEST_INVALID" | "BAKE_REQUEST_INVALID" => {
                envelope.scope.as_deref() == Some("candidate") && envelope.retryable == Some(false)
            }
            _ => false,
        }
    }

    let envelope = serde_json::from_str::<ErrorEnvelope>(&body_text).ok();
    let structured_code = envelope
        .as_ref()
        .filter(|value| has_valid_contract(value))
        .and_then(|value| value.code.as_deref());
    let (mapped_status, code) = match structured_code {
        Some("BAKE_OUTPUT_TRUNCATED") => {
            (StatusCode::UNPROCESSABLE_ENTITY, "BAKE_OUTPUT_TRUNCATED")
        }
        Some("BAKE_OUTPUT_INVALID") => (StatusCode::UNPROCESSABLE_ENTITY, "BAKE_OUTPUT_INVALID"),
        Some("BAKE_MODEL_RESPONSE_INVALID") => (
            StatusCode::UNPROCESSABLE_ENTITY,
            "BAKE_MODEL_RESPONSE_INVALID",
        ),
        Some("BAKE_MODEL_REQUEST_INVALID") => (
            StatusCode::UNPROCESSABLE_ENTITY,
            "BAKE_MODEL_REQUEST_INVALID",
        ),
        Some("BAKE_REQUEST_INVALID") => (StatusCode::BAD_REQUEST, "BAKE_REQUEST_INVALID"),
        Some("INFERENCE_TIMEOUT") => (StatusCode::GATEWAY_TIMEOUT, "INFERENCE_TIMEOUT"),
        Some("INFERENCE_PREEMPTED") => (StatusCode::SERVICE_UNAVAILABLE, "INFERENCE_PREEMPTED"),
        Some("MODEL_RATE_LIMITED") => (StatusCode::SERVICE_UNAVAILABLE, "MODEL_RATE_LIMITED"),
        Some("MODEL_UNAVAILABLE") => (StatusCode::SERVICE_UNAVAILABLE, "MODEL_UNAVAILABLE"),
        Some("SIDECAR_UNAVAILABLE") => (StatusCode::SERVICE_UNAVAILABLE, "SIDECAR_UNAVAILABLE"),
        Some("BAKE_MODEL_UPSTREAM_ERROR") => (StatusCode::BAD_GATEWAY, "BAKE_MODEL_UPSTREAM_ERROR"),
        Some("BAKE_INTERNAL_ERROR") => (StatusCode::INTERNAL_SERVER_ERROR, "BAKE_INTERNAL_ERROR"),
        _ => match status.as_u16() {
            400 | 422 => (StatusCode::BAD_REQUEST, "BAD_REQUEST"),
            504 => (StatusCode::GATEWAY_TIMEOUT, "INFERENCE_TIMEOUT"),
            code if code >= 500 => (
                StatusCode::from_u16(code).unwrap_or(StatusCode::BAD_GATEWAY),
                "BAKE_UNCLASSIFIED_UPSTREAM_ERROR",
            ),
            _ => (StatusCode::BAD_GATEWAY, "BAKE_UNCLASSIFIED_UPSTREAM_ERROR"),
        },
    };

    // Sidecar 原始 body 可能包含供应商模型名或底层响应，不进入客户端错误详情。
    let message = format!("{service_name}返回错误 ({status}, code={code})");

    BakeSidecarError {
        status: mapped_status,
        code,
        message,
    }
}

fn resolve_review_status(
    _value: Option<&str>,
    _match_score: Option<f64>,
    _match_level: Option<&str>,
) -> String {
    "auto_created".to_string()
}

#[derive(Debug)]
struct KnowledgeDecision {
    state: &'static str,
    score: Option<f64>,
    reason_code: &'static str,
    reason_summary: String,
}

fn resolve_knowledge_decision(payload: &BakeKnowledgeArtifactPayload) -> KnowledgeDecision {
    let score = payload.match_score.filter(|value| value.is_finite());
    let future_question = payload
        .future_question
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let decision_reason = payload
        .decision_reason
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let evidence = payload
        .evidence_summary
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty());

    if score.is_some_and(|value| value < KNOWLEDGE_SHADOW_SCORE) {
        return KnowledgeDecision {
            state: "timeline_only",
            score,
            reason_code: "below_shadow_threshold",
            reason_summary: decision_reason
                .unwrap_or("未来复用价值不足，保留在原始时间线中")
                .to_string(),
        };
    }
    if score.is_none()
        || score.is_some_and(|value| value < KNOWLEDGE_PUBLISH_SCORE)
        || future_question.is_none()
        || decision_reason.is_none()
        || evidence.is_none()
    {
        return KnowledgeDecision {
            state: "shadow",
            score,
            reason_code: if score.is_none() {
                "quality_score_missing"
            } else if score.is_some_and(|value| value < KNOWLEDGE_PUBLISH_SCORE) {
                "below_publish_threshold"
            } else {
                "open_semantic_evidence_incomplete"
            },
            reason_summary: decision_reason
                .unwrap_or("未来问题、复用理由或事实证据尚不完整，进入 shadow 复核")
                .to_string(),
        };
    }
    KnowledgeDecision {
        state: "published",
        score,
        reason_code: "publish_threshold_met",
        reason_summary: decision_reason.unwrap_or_default().to_string(),
    }
}

fn knowledge_gate_outcome(
    payload: &BakeKnowledgeArtifactPayload,
    decision: &KnowledgeDecision,
) -> CandidatePersistResult {
    CandidatePersistResult::knowledge_gated(
        decision.state,
        decision.score,
        decision.reason_code,
        decision.reason_summary.clone(),
        if decision.state == "shadow" {
            serde_json::to_string(payload).ok()
        } else {
            None
        },
    )
}

fn collect_current_document_source_timeline_ids(
    records: &[BakeDocumentRecord],
) -> std::collections::HashSet<i64> {
    records
        .iter()
        .filter(|record| is_current_bake_document(record))
        .flat_map(|record| {
            parse_json_vec_string(&record.source_memory_ids)
                .into_iter()
                .filter_map(|value| value.parse::<i64>().ok())
        })
        .collect()
}

fn normalize_doc_url(url: &str) -> String {
    let trimmed = url.trim();
    let no_fragment = trimmed.split('#').next().unwrap_or(trimmed);
    let no_query = no_fragment.split('?').next().unwrap_or(no_fragment);
    no_query.trim_end_matches('/').to_string()
}

fn document_urls_compatible_for_title_match(
    existing_url: Option<&str>,
    candidate_url: Option<&str>,
) -> bool {
    match (
        existing_url.and_then(canonical_document_identity),
        candidate_url.and_then(canonical_document_identity),
    ) {
        (Some(existing), Some(candidate)) => existing == candidate,
        _ => true,
    }
}

fn artifact_source_fingerprint(candidate: &BakeMemorySourceRecord) -> Option<String> {
    let source_text = candidate
        .url_aggregated_text
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
        .or_else(|| {
            let parts = [
                candidate.capture_ax_text.as_deref(),
                candidate.capture_ocr_text.as_deref(),
                candidate.capture_input_text.as_deref(),
                candidate.capture_audio_text.as_deref(),
            ]
            .into_iter()
            .flatten()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .collect::<Vec<_>>();
            (!parts.is_empty()).then(|| parts.join("\n"))
        })?;
    // 与文档刷新抓取同一条归一化路径，保证浏览器刷新抓到的正文
    // 与历史 capture 的指纹可以直接比较。
    source_text_fingerprint(&source_text)
}

fn document_source_title(candidate: &BakeMemorySourceRecord) -> Option<String> {
    if !document_evidence(candidate).allows_auto_create {
        return None;
    }
    candidate
        .preferred_source_title
        .as_deref()
        .and_then(|title| {
            canonical_document_source_title(title, candidate.capture_app_name.as_deref())
        })
        .or_else(|| {
            candidate
                .capture_webpage_title
                .as_deref()
                .and_then(|title| {
                    canonical_document_source_title(title, candidate.capture_app_name.as_deref())
                })
        })
        .or_else(|| {
            candidate.capture_win_title.as_deref().and_then(|title| {
                canonical_document_source_title(title, candidate.capture_app_name.as_deref())
            })
        })
}

fn sanitize_generated_document_title(value: &str) -> String {
    let title = value.trim();
    for prefix in ["文档增量", "增量文档", "文档更新", "文档补充", "新增文档"] {
        if let Some(rest) = title.strip_prefix(prefix) {
            let rest = rest.trim_start();
            if let Some(cleaned) = rest.strip_prefix(['：', ':', '-', '—']) {
                let cleaned = cleaned.trim();
                if !cleaned.is_empty() {
                    return cleaned.to_string();
                }
            }
        }
    }
    title.to_string()
}

fn generated_title_uses_incremental_wording(value: &str) -> bool {
    sanitize_generated_document_title(value) != value.trim()
}

fn normalize_optional_url(url: Option<String>) -> Option<String> {
    url.map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn collect_source_capture_id_strings(
    storage: &StorageManager,
    source: &BakeMemorySourceRecord,
) -> Result<Vec<String>, ApiError> {
    let timeline_capture_ids = storage.get_timeline_capture_ids(source.timeline.id)?;
    Ok(merge_string_lists(
        source_capture_id_strings(source),
        &timeline_capture_ids
            .into_iter()
            .map(|id| id.to_string())
            .collect::<Vec<_>>(),
    ))
}

fn source_capture_id_strings(source: &BakeMemorySourceRecord) -> Vec<String> {
    let mut ids = source
        .timeline
        .capture_ids
        .as_deref()
        .map(parse_json_vec_string_lossy)
        .unwrap_or_default();
    let primary = source.timeline.capture_id.to_string();
    if !ids.contains(&primary) {
        ids.insert(0, primary);
    }
    for record in &source.action_trace {
        let capture_id = record.capture_id.to_string();
        if !ids.contains(&capture_id) {
            ids.push(capture_id);
        }
    }
    ids
}

fn build_bake_knowledge_entry(
    source: &BakeMemorySourceRecord,
    payload: &BakeKnowledgeArtifactPayload,
    review_status: &str,
    trigger_reason: &str,
    source_capture_ids: &[String],
) -> Result<NewBakeKnowledge, ApiError> {
    let entities = if payload.entities.is_empty() {
        parse_json_vec_string(&source.timeline.entities)
    } else {
        payload.entities.clone()
    };
    let details = json!({
        "source_timeline_id": source.timeline.id,
        "source_memory_ids": [source.timeline.id.to_string()],
        "source_capture_ids": source_capture_ids,
        "source_timeline_ids": [source.timeline.id.to_string()],
        "episode_cluster_id": source.timeline.capture_id.to_string(),
        "match_score": payload.match_score,
        "match_level": payload.match_level.clone(),
        "creation_mode": "llm_bake",
        "review_status": review_status,
        "evidence_summary": payload.evidence_summary.clone(),
        "generation_version": BAKE_GENERATION_VERSION,
        "trigger_reason": trigger_reason,
        "status": review_status,
        "source_title": source.timeline.summary.clone(),
    });
    Ok(NewBakeKnowledge {
        timeline_id: source.timeline.id,
        title: knowledge_title_from_payload(payload),
        summary: knowledge_summary_from_payload(payload),
        content: Some(details.to_string()),
        detailed_content: payload.details.clone(),
        entities: to_json_string(&entities)?,
        importance: payload
            .importance
            .unwrap_or(source.timeline.importance)
            .max(1),
        source_capture_ids: Some(to_json_string(&source_capture_ids)?),
    })
}

fn deterministic_document_recovery(
    candidate: &BakeMemorySourceRecord,
) -> Option<BakeArtifactExtraction> {
    if !document_evidence(candidate).allows_auto_create {
        return None;
    }
    let title = document_source_title(candidate)?;
    let full_content = candidate
        .url_aggregated_text
        .as_deref()
        .or(candidate.capture_ax_text.as_deref())
        .or(candidate.capture_ocr_text.as_deref())
        .map(str::trim)
        .filter(|text| text.chars().count() >= 200)?
        .to_string();
    Some(BakeArtifactExtraction {
        accepted: true,
        reason: Some("deterministic_document_source_recovery".to_string()),
        payload: Some(json!({
            "name": title,
            "category": "来源文档",
            "summary": null,
            "full_content": full_content,
            "details": null,
            "status": "candidate",
            "tags": [],
            "applicable_tasks": [],
            "structure_sections": [],
            "style_phrases": [],
            "replacement_rules": [],
            "review_status": "candidate",
            "evidence_summary": "由已采集的文档标题与可见正文恢复，未生成额外内容"
        })),
    })
}

fn build_bake_document(
    source: &BakeMemorySourceRecord,
    payload: &BakeDocumentArtifactPayload,
    review_status: &str,
    source_capture_ids: &[String],
    linked_knowledge_ids: &[String],
) -> Result<NewBakeDocument, ApiError> {
    let tags = if payload.tags.is_empty() {
        parse_json_vec_string(&source.timeline.entities)
    } else {
        payload.tags.clone()
    };
    let source_memory_ids = vec![source.timeline.id.to_string()];
    let full_content = payload
        .full_content
        .clone()
        .filter(|s| !s.trim().is_empty())
        .or_else(|| payload.details.clone());

    let content_hash = full_content.as_ref().map(|content| {
        let mut hasher = Sha256::new();
        hasher.update(content.as_bytes());
        format!("{:x}", hasher.finalize())
    });

    let structured_content = json!({
        "sections": payload.sections,
        "style_phrases": payload.style_phrases,
        "replacement_rules": payload.replacement_rules,
        "usage_notes": payload.details,
    })
    .to_string();
    Ok(NewBakeDocument {
        title: payload.title.clone(),
        doc_type: payload
            .doc_type
            .clone()
            .unwrap_or_else(|| "文档模板".to_string()),
        status: payload
            .status
            .clone()
            .filter(|status| status != "draft")
            .unwrap_or_else(|| "enabled".to_string()),
        tags: to_json_string(&tags)?,
        applicable_tasks: to_json_string(&payload.applicable_tasks)?,
        source_memory_ids: to_json_string(&source_memory_ids)?,
        source_capture_ids: to_json_string(&source_capture_ids)?,
        source_episode_ids: to_json_string(&source_memory_ids)?,
        linked_knowledge_ids: to_json_string(&linked_knowledge_ids)?,
        sections_json: to_json_string(&payload.sections)?,
        style_phrases: to_json_string(&payload.style_phrases)?,
        replacement_rules: to_json_string(&payload.replacement_rules)?,
        summary: payload.summary.clone(),
        full_content,
        structured_content,
        prompt_hint: payload.prompt_hint.clone(),
        diagram_code: payload.diagram_code.clone(),
        image_assets: "[]".to_string(),
        source_app_name: source.capture_app_name.clone(),
        source_win_title: document_source_title(source)
            .or_else(|| source.capture_win_title.clone()),
        source_url: source.capture_url.clone(),
        content_hash,
        language: None,
        usage_count: 0,
        match_score: payload.match_score,
        match_level: payload.match_level.clone(),
        creation_mode: "llm_bake".to_string(),
        review_status: review_status.to_string(),
        evidence_summary: payload.evidence_summary.clone(),
        generation_version: Some(BAKE_GENERATION_VERSION.to_string()),
        deleted_at: None,
    })
}

fn build_bake_sop_entry(
    source: &BakeMemorySourceRecord,
    payload: &BakeSopArtifactPayload,
    review_status: &str,
    trigger_reason: &str,
    source_capture_ids: &[String],
) -> Result<NewBakeSop, ApiError> {
    let trigger_keywords = if payload.trigger_keywords.is_empty() {
        parse_json_vec_string(&source.timeline.entities)
    } else {
        payload.trigger_keywords.clone()
    };
    let linked_knowledge_ids = if payload.linked_knowledge_ids.is_empty() {
        vec![source.timeline.id.to_string()]
    } else {
        payload.linked_knowledge_ids.clone()
    };
    let action_capture_ids = source
        .action_trace
        .iter()
        .filter(|record| record.evidence_role.as_deref() == Some("action"))
        .map(|record| record.capture_id.to_string())
        .collect::<Vec<_>>();
    let result_capture_ids = source
        .action_trace
        .iter()
        .filter(|record| record.evidence_role.as_deref() == Some("result"))
        .map(|record| record.capture_id.to_string())
        .collect::<Vec<_>>();
    let source_capture_id_set = source_capture_ids.iter().cloned().collect::<HashSet<_>>();
    let step_evidence = payload
        .steps
        .iter()
        .enumerate()
        .map(|(index, step)| {
            let step_index = index as i64 + 1;
            let mut capture_ids = payload
                .step_evidence
                .iter()
                .find(|item| item.step_index == step_index)
                .map(|item| {
                    item.capture_ids
                        .iter()
                        .filter(|capture_id| source_capture_id_set.contains(*capture_id))
                        .cloned()
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            capture_ids.sort();
            capture_ids.dedup();
            json!({
                "step_index": step_index,
                "step": step,
                "capture_ids": capture_ids,
            })
        })
        .collect::<Vec<_>>();
    let details = json!({
        "source_timeline_id": source.timeline.id,
        "source_memory_ids": [source.timeline.id.to_string()],
        "source_capture_ids": source_capture_ids,
        "match_score": payload.match_score,
        "match_level": payload.match_level.clone(),
        "creation_mode": "llm_bake",
        "review_status": review_status,
        "evidence_summary": payload.evidence_summary.clone(),
        "generation_version": BAKE_GENERATION_VERSION,
        "trigger_reason": trigger_reason,
        "source_capture_id": source.timeline.capture_id.to_string(),
        "source_title": payload.source_title.clone().unwrap_or_else(|| source.timeline.summary.clone()),
        "trigger_keywords": trigger_keywords,
        "confidence": payload.confidence.clone().unwrap_or_else(|| infer_confidence(source.timeline.importance, source.timeline.occurrence_count)),
        "extracted_problem": payload.extracted_problem.clone(),
        "steps": payload.steps,
        "step_evidence": step_evidence,
        "step_evidence_mode": "model_aligned",
        "sop_evidence_contract": "sop-evidence.v2",
        "sop_evidence_mode": sop_eligibility(source).mode.map(SopEvidenceMode::as_str),
        "action_capture_ids": action_capture_ids,
        "result_capture_ids": result_capture_ids,
        "linked_knowledge_ids": linked_knowledge_ids,
        "status": review_status,
    });
    Ok(NewBakeSop {
        timeline_id: source.timeline.id,
        title: payload
            .overview
            .clone()
            .unwrap_or_else(|| payload.summary.clone()),
        summary: payload.summary.clone(),
        content: Some(details.to_string()),
        detailed_content: Some(render_bake_sop_markdown(payload)),
        entities: source.timeline.entities.clone(),
        importance: source.timeline.importance.max(3),
        source_capture_ids: Some(to_json_string(&source_capture_ids)?),
    })
}

fn render_bake_sop_markdown(payload: &BakeSopArtifactPayload) -> String {
    let scenario = payload
        .extracted_problem
        .as_deref()
        .or(payload.overview.as_deref())
        .unwrap_or(payload.summary.as_str())
        .trim();
    let route = payload
        .steps
        .iter()
        .enumerate()
        .map(|(index, step)| format!("{}. {}", index + 1, step.trim()))
        .collect::<Vec<_>>()
        .join("\n");
    let verification = payload
        .evidence_summary
        .as_deref()
        .filter(|value| !value.trim().is_empty())
        .or_else(|| payload.steps.last().map(String::as_str))
        .unwrap_or("按最后一步确认预期结果")
        .trim();
    format!("## 适用场景\n\n{scenario}\n\n## 行动路线\n\n{route}\n\n## 验证方式\n\n{verification}")
}

fn map_bake_run_record(record: BakeRunRecord) -> BakeRunPayload {
    BakeRunPayload {
        id: record.id.to_string(),
        trigger_reason: record.trigger_reason,
        status: record.status,
        started_at: record.started_at,
        completed_at: record.completed_at,
        processed_episode_count: record.processed_episode_count,
        auto_created_count: record.auto_created_count,
        candidate_count: record.candidate_count,
        discarded_count: record.discarded_count,
        knowledge_created_count: record.knowledge_created_count,
        document_created_count: record.document_created_count,
        sop_created_count: record.sop_created_count,
        error_message: record.error_message,
        latency_ms: record.latency_ms,
    }
}

fn format_bake_run_activity(run: &BakeRunRecord) -> String {
    let summary = format!(
        "自动 {}，候选 {}，丢弃 {}",
        run.auto_created_count, run.candidate_count, run.discarded_count
    );
    match run.trigger_reason.as_str() {
        "knowledge_background" => format!("知识后台提炼后已自动执行分类烤面包（{}）", summary),
        "manual_debug" => format!("手动触发分类烤面包执行完成（{}）", summary),
        other => format!("分类提炼执行完成：{}（{}）", other, summary),
    }
}

fn map_capture_record(
    record: CaptureRecord,
    linked_timeline: Option<&(i64, String)>,
) -> BakeCapturePayload {
    let best_text = record.best_text().map(ToString::to_string);
    let summary = record.win_title.clone().or_else(|| {
        best_text
            .as_ref()
            .map(|text| text.chars().take(80).collect::<String>())
    });
    let semantic_type_label = infer_semantic_type_label(&record);
    let raw_type_label = friendly_raw_type_label(&record.event_type, &record);

    BakeCapturePayload {
        id: record.id.to_string(),
        ts: record.ts,
        app_name: record.app_name,
        app_bundle_id: record.app_bundle_id,
        win_title: record.win_title,
        event_type: record.event_type,
        semantic_type_label,
        raw_type_label,
        ax_text: record.ax_text,
        ax_focused_role: record.ax_focused_role,
        ax_focused_id: record.ax_focused_id,
        ocr_text: record.ocr_text,
        input_text: record.input_text,
        audio_text: record.audio_text,
        screenshot_path: record.screenshot_path,
        screenshot_source: record.screenshot_source,
        url: record.url,
        webpage_title: record.webpage_title,
        is_sensitive: record.is_sensitive,
        pii_scrubbed: record.pii_scrubbed,
        best_text,
        summary,
        linked_timeline_id: linked_timeline.map(|(id, _)| id.to_string()),
        linked_timeline_summary: linked_timeline.map(|(_, summary)| summary.clone()),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreateOrUpdateKnowledgeRequest {
    pub summary: String,
    pub overview: Option<String>,
    pub detailed_content: Option<String>,
    #[serde(default = "default_manual_importance")]
    pub importance: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreateOrUpdateSopRequest {
    pub extracted_problem: String,
    pub detailed_content: Option<String>,
    #[serde(default)]
    pub steps: Vec<String>,
    #[serde(default)]
    pub trigger_keywords: Vec<String>,
}

fn default_manual_importance() -> i64 {
    5
}

fn normalize_optional_text(value: Option<String>) -> Option<String> {
    value
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
}

fn parse_exact_list_id(query: &str) -> Option<i64> {
    let value = query.trim().strip_prefix('#').unwrap_or(query.trim());
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    value.parse::<i64>().ok()
}

fn validate_knowledge_request(payload: &CreateOrUpdateKnowledgeRequest) -> Result<(), ApiError> {
    if payload.summary.trim().is_empty() {
        return Err(ApiError::BadRequest("知识标题不能为空".to_string()));
    }
    Ok(())
}

fn validate_sop_request(payload: &CreateOrUpdateSopRequest) -> Result<(), ApiError> {
    if payload.extracted_problem.trim().is_empty() {
        return Err(ApiError::BadRequest("操作名称不能为空".to_string()));
    }
    if !payload.steps.iter().any(|step| !step.trim().is_empty()) {
        return Err(ApiError::BadRequest("至少需要一个操作步骤".to_string()));
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreateOrUpdateDocumentRequest {
    pub title: String,
    pub doc_type: String,
    pub status: String,
    pub tags: Vec<String>,
    pub applicable_tasks: Vec<String>,
    #[serde(default)]
    pub source_memory_ids: Vec<String>,
    #[serde(default)]
    pub source_capture_ids: Vec<String>,
    #[serde(default)]
    pub source_episode_ids: Vec<String>,
    #[serde(default)]
    pub linked_knowledge_ids: Vec<String>,
    #[serde(default)]
    pub sections: Vec<DocumentSectionPayload>,
    #[serde(default)]
    pub style_phrases: Vec<String>,
    #[serde(default)]
    pub replacement_rules: Vec<ReplacementRulePayload>,
    pub summary: Option<String>,
    pub full_content: Option<String>,
    #[serde(default)]
    pub structured_content: Option<String>,
    pub prompt_hint: Option<String>,
    pub diagram_code: Option<String>,
    #[serde(default)]
    pub image_assets: Vec<String>,
    pub source_app_name: Option<String>,
    pub source_win_title: Option<String>,
    pub source_url: Option<String>,
    pub content_hash: Option<String>,
    pub language: Option<String>,
    pub usage_count: Option<i64>,
    pub match_score: Option<f64>,
    pub match_level: Option<String>,
    pub creation_mode: Option<String>,
    pub review_status: Option<String>,
    pub evidence_summary: Option<String>,
    pub generation_version: Option<String>,
    pub deleted_at: Option<i64>,
}

fn request_to_new_document(
    payload: CreateOrUpdateDocumentRequest,
) -> Result<NewBakeDocument, ApiError> {
    Ok(NewBakeDocument {
        title: payload.title,
        doc_type: payload.doc_type,
        status: payload.status,
        tags: to_json_string(&payload.tags)?,
        applicable_tasks: to_json_string(&payload.applicable_tasks)?,
        source_memory_ids: to_json_string(&payload.source_memory_ids)?,
        source_capture_ids: to_json_string(&payload.source_capture_ids)?,
        source_episode_ids: to_json_string(&payload.source_episode_ids)?,
        linked_knowledge_ids: to_json_string(&payload.linked_knowledge_ids)?,
        sections_json: to_json_string(&payload.sections)?,
        style_phrases: to_json_string(&payload.style_phrases)?,
        replacement_rules: to_json_string(&payload.replacement_rules)?,
        summary: payload.summary,
        full_content: payload.full_content,
        structured_content: payload
            .structured_content
            .unwrap_or_else(|| "{}".to_string()),
        prompt_hint: payload.prompt_hint,
        diagram_code: payload.diagram_code,
        image_assets: to_json_string(&payload.image_assets)?,
        source_app_name: payload.source_app_name,
        source_win_title: payload.source_win_title,
        source_url: payload.source_url,
        content_hash: payload.content_hash,
        language: payload.language,
        usage_count: payload.usage_count.unwrap_or(0),
        match_score: payload.match_score,
        match_level: payload.match_level,
        creation_mode: payload
            .creation_mode
            .unwrap_or_else(|| "manual".to_string()),
        review_status: payload.review_status.unwrap_or_else(|| "draft".to_string()),
        evidence_summary: payload.evidence_summary,
        generation_version: payload.generation_version,
        deleted_at: payload.deleted_at,
    })
}

fn bake_document_record_to_new(record: BakeDocumentRecord) -> NewBakeDocument {
    NewBakeDocument {
        title: record.title,
        doc_type: record.doc_type,
        status: record.status,
        tags: record.tags,
        applicable_tasks: record.applicable_tasks,
        source_memory_ids: record.source_memory_ids,
        source_capture_ids: record.source_capture_ids,
        source_episode_ids: record.source_episode_ids,
        linked_knowledge_ids: record.linked_knowledge_ids,
        sections_json: record.sections_json,
        style_phrases: record.style_phrases,
        replacement_rules: record.replacement_rules,
        summary: record.summary,
        full_content: record.full_content,
        structured_content: record.structured_content,
        prompt_hint: record.prompt_hint,
        diagram_code: record.diagram_code,
        image_assets: record.image_assets,
        source_app_name: record.source_app_name,
        source_win_title: record.source_win_title,
        source_url: record.source_url,
        content_hash: record.content_hash,
        language: record.language,
        usage_count: record.usage_count,
        match_score: record.match_score,
        match_level: record.match_level,
        creation_mode: record.creation_mode,
        review_status: record.review_status,
        evidence_summary: record.evidence_summary,
        generation_version: record.generation_version,
        deleted_at: record.deleted_at,
    }
}

fn map_document_record(record: BakeDocumentRecord, is_favorite: bool) -> BakeDocumentPayload {
    use chrono::{DateTime, Local, Utc};
    let created_at = DateTime::<Utc>::from_timestamp(record.created_at / 1000, 0)
        .map(|dt| {
            dt.with_timezone(&Local)
                .format("%Y-%m-%d %H:%M:%S")
                .to_string()
        })
        .unwrap_or_else(|| record.created_at.to_string());
    let updated_at = DateTime::<Utc>::from_timestamp(record.updated_at / 1000, 0)
        .map(|dt| {
            dt.with_timezone(&Local)
                .format("%Y-%m-%d %H:%M:%S")
                .to_string()
        })
        .unwrap_or_else(|| record.updated_at.to_string());

    BakeDocumentPayload {
        id: record.id.to_string(),
        is_favorite,
        title: record.title,
        doc_type: record.doc_type,
        status: record.status,
        tags: parse_json_vec_string(&record.tags),
        applicable_tasks: parse_json_vec_string(&record.applicable_tasks),
        source_memory_ids: parse_json_vec_string(&record.source_memory_ids),
        source_capture_ids: parse_json_vec_string(&record.source_capture_ids),
        source_episode_ids: parse_json_vec_string(&record.source_episode_ids),
        linked_knowledge_ids: parse_json_vec_string(&record.linked_knowledge_ids),
        sections: serde_json::from_str(&record.sections_json).unwrap_or_default(),
        style_phrases: parse_json_vec_string(&record.style_phrases),
        replacement_rules: serde_json::from_str(&record.replacement_rules).unwrap_or_default(),
        summary: record.summary,
        full_content: record.full_content,
        prompt_hint: record.prompt_hint,
        diagram_code: record.diagram_code,
        image_assets: parse_json_vec_string(&record.image_assets),
        source_url: record.source_url,
        usage_count: record.usage_count,
        match_score: record.match_score,
        match_level: record.match_level,
        creation_mode: record.creation_mode,
        review_status: record.review_status,
        evidence_summary: record.evidence_summary,
        generation_version: record.generation_version,
        refresh_policy: record.refresh_policy,
        last_refresh_checked_at_ms: record.last_refresh_checked_at_ms,
        last_refresh_error: record.last_refresh_error,
        last_refresh_success_at_ms: record.last_refresh_success_at_ms,
        last_refresh_status: record.last_refresh_status,
        last_refresh_completeness: record.last_refresh_completeness,
        last_refresh_content_hash: record.last_refresh_content_hash,
        last_refresh_character_count: record.last_refresh_character_count,
        last_refresh_segment_count: record.last_refresh_segment_count,
        last_refresh_truncated: record.last_refresh_truncated,
        deleted_at: record.deleted_at,
        created_at,
        created_at_ms: record.created_at,
        updated_at,
        updated_at_ms: record.updated_at,
    }
}

fn map_memory_record(record: TimelineRecord, capture_url: Option<String>) -> BakeMemoryPayload {
    let details = parse_details(record.details.as_deref());
    let tags = details
        .get("tags")
        .and_then(|value| serde_json::from_value::<Vec<String>>(value.clone()).ok())
        .unwrap_or_else(|| parse_json_vec_string(&record.entities));

    let capture_ids = record
        .capture_ids
        .as_deref()
        .and_then(|s| serde_json::from_str::<Vec<i64>>(s).ok())
        .unwrap_or_default();

    BakeMemoryPayload {
        id: record.id.to_string(),
        title: record.summary,
        url: details
            .get("url")
            .and_then(Value::as_str)
            .and_then(|value| normalize_optional_url(Some(value.to_string())))
            .or(capture_url),
        source_capture_id: details
            .get("source_capture_id")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .or_else(|| Some(record.capture_id.to_string())),
        source_timeline_id: details
            .get("source_timeline_id")
            .or_else(|| details.get("source_knowledge_id"))
            .and_then(Value::as_i64)
            .map(|value| value.to_string()),
        details: details
            .get("description")
            .or_else(|| details.get("source_timeline_details"))
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .or_else(|| {
                record.details.as_ref().and_then(|raw| {
                    if serde_json::from_str::<Value>(raw).is_ok() {
                        None
                    } else {
                        Some(raw.clone())
                    }
                })
            }),
        summary: record.overview,
        weight: details
            .get("weight")
            .and_then(Value::as_i64)
            .unwrap_or(record.importance * 20),
        open_count: details
            .get("open_count")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        dwell_seconds: details
            .get("dwell_seconds")
            .and_then(Value::as_i64)
            .unwrap_or(0),
        has_edit_action: details
            .get("has_edit_action")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        knowledge_ref_count: details
            .get("knowledge_ref_count")
            .and_then(Value::as_i64)
            .or(record.occurrence_count)
            .unwrap_or(0),
        status: details
            .get("status")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .unwrap_or_else(|| {
                if record.user_verified {
                    "confirmed".to_string()
                } else {
                    "candidate".to_string()
                }
            }),
        suggested_action: details
            .get("suggested_action")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .or_else(|| infer_suggested_action(&tags)),
        tags,
        last_visited_at: details
            .get("last_visited_at")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        created_at: record.created_at,
        created_at_ms: record.created_at_ms,
        knowledge_match_score: details.get("knowledge_match_score").and_then(Value::as_f64),
        knowledge_match_level: details
            .get("knowledge_match_level")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        template_match_score: details.get("template_match_score").and_then(Value::as_f64),
        template_match_level: details
            .get("template_match_level")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        sop_match_score: details.get("sop_match_score").and_then(Value::as_f64),
        sop_match_level: details
            .get("sop_match_level")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        capture_ids,
        key_timestamps: record
            .key_timestamps
            .as_deref()
            .and_then(|s| serde_json::from_str(s).ok()),
    }
}

fn bake_knowledge_record_to_timeline(record: BakeKnowledgeRecord) -> TimelineRecord {
    let capture_id = record
        .source_capture_ids
        .as_deref()
        .map(parse_json_vec_string_lossy)
        .unwrap_or_default()
        .into_iter()
        .find_map(|value| value.parse::<i64>().ok())
        .filter(|value| *value > 0)
        .unwrap_or(0);
    let mut details = parse_details(record.content.as_deref())
        .as_object()
        .cloned()
        .unwrap_or_default();
    if record.timeline_id > 0
        && details
            .get("source_timeline_id")
            .and_then(|value| {
                value
                    .as_i64()
                    .or_else(|| {
                        value
                            .as_str()
                            .and_then(|value| value.trim().parse::<i64>().ok())
                    })
                    .filter(|value| *value > 0)
            })
            .is_none()
    {
        details.insert("source_timeline_id".to_string(), json!(record.timeline_id));
    }
    TimelineRecord {
        id: record.id,
        capture_id,
        summary: record.summary,
        overview: Some(record.title),
        details: Some(Value::Object(details).to_string()),
        detailed_content: record.detailed_content,
        entities: record.entities,
        category: CATEGORY_BAKE_KNOWLEDGE.to_string(),
        importance: record.importance,
        occurrence_count: Some(record.occurrence_count),
        observed_at: None,
        event_time_start: None,
        event_time_end: None,
        history_view: false,
        content_origin: None,
        activity_type: None,
        is_self_generated: false,
        evidence_strength: None,
        user_verified: record.user_verified,
        user_edited: record.user_edited,
        created_at: record.created_at,
        updated_at: record.updated_at,
        created_at_ms: record.created_at_ms,
        updated_at_ms: record.updated_at_ms,
        capture_ids: record.source_capture_ids,
        start_time: None,
        end_time: None,
        duration_minutes: None,
        frag_app_name: None,
        frag_win_title: None,
        time_range_start: None,
        time_range_end: None,
        key_timestamps: None,
    }
}

fn bake_sop_record_to_timeline(record: BakeSopRecord) -> TimelineRecord {
    TimelineRecord {
        id: record.id,
        capture_id: record.timeline_id,
        summary: record.summary,
        overview: Some(record.title),
        details: record.content,
        detailed_content: record.detailed_content,
        entities: record.entities,
        category: CATEGORY_BAKE_SOP.to_string(),
        importance: record.importance,
        occurrence_count: None,
        observed_at: None,
        event_time_start: None,
        event_time_end: None,
        history_view: false,
        content_origin: None,
        activity_type: None,
        is_self_generated: false,
        evidence_strength: None,
        user_verified: record.user_verified,
        user_edited: record.user_edited,
        created_at: record.created_at,
        updated_at: record.updated_at,
        created_at_ms: record.created_at_ms,
        updated_at_ms: record.updated_at_ms,
        capture_ids: record.source_capture_ids,
        start_time: None,
        end_time: None,
        duration_minutes: None,
        frag_app_name: None,
        frag_win_title: None,
        time_range_start: None,
        time_range_end: None,
        key_timestamps: None,
    }
}

fn map_bake_knowledge_record(record: TimelineRecord) -> BakeKnowledgePayload {
    let details = parse_details(record.details.as_deref());
    let status = extract_status_from_details(&details, record.user_verified);
    let review_status = details
        .get("review_status")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or_else(|| status.clone());
    BakeKnowledgePayload {
        id: record.id.to_string(),
        is_favorite: false,
        capture_id: record.capture_id.to_string(),
        source_capture_ids: record
            .capture_ids
            .as_deref()
            .map(parse_json_vec_string_lossy)
            .unwrap_or_default(),
        source_timeline_id: details
            .get("source_timeline_id")
            .or_else(|| details.get("source_knowledge_id"))
            .and_then(|value| {
                value
                    .as_i64()
                    .map(|id| id.to_string())
                    .or_else(|| value.as_str().map(ToString::to_string))
            })
            .unwrap_or_default(),
        source_url: None,
        summary: record.summary,
        overview: record.overview,
        details: record.details,
        detailed_content: record.detailed_content,
        entities: parse_json_vec_string(&record.entities),
        category: record.category,
        importance: record.importance,
        occurrence_count: record.occurrence_count.unwrap_or(0),
        observed_at: record.observed_at,
        status,
        review_status,
        match_score: details.get("match_score").and_then(Value::as_f64),
        match_level: details
            .get("match_level")
            .and_then(Value::as_str)
            .map(ToString::to_string),
        created_at: record.created_at,
        created_at_ms: record.created_at_ms,
        updated_at: record.updated_at,
        updated_at_ms: record.updated_at_ms,
    }
}

fn map_sop_record_with_linked_summaries(
    storage: &StorageManager,
    record: TimelineRecord,
) -> BakeSopPayload {
    let details = parse_details(record.details.as_deref());
    let linked_knowledge_ids = details
        .get("linked_knowledge_ids")
        .and_then(|value| serde_json::from_value::<Vec<String>>(value.clone()).ok())
        .unwrap_or_default();
    let linked_knowledge_summaries =
        resolve_linked_knowledge_summaries(storage, &linked_knowledge_ids);

    BakeSopPayload {
        id: record.id.to_string(),
        is_favorite: false,
        source_capture_id: details
            .get("source_capture_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        source_timeline_id: details
            .get("source_timeline_id")
            .or_else(|| details.get("source_knowledge_id"))
            .and_then(|value| {
                value
                    .as_i64()
                    .map(|id| id.to_string())
                    .or_else(|| value.as_str().map(ToString::to_string))
            })
            .unwrap_or_else(|| record.id.to_string()),
        source_title: details
            .get("source_title")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .or_else(|| Some(record.summary.clone())),
        trigger_keywords: details
            .get("trigger_keywords")
            .and_then(|value| serde_json::from_value::<Vec<String>>(value.clone()).ok())
            .unwrap_or_else(|| parse_json_vec_string(&record.entities)),
        confidence: details
            .get("confidence")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .unwrap_or_else(|| infer_confidence(record.importance, record.occurrence_count)),
        extracted_problem: Some(record.summary),
        detailed_content: record.detailed_content,
        steps: details
            .get("steps")
            .and_then(|value| serde_json::from_value::<Vec<String>>(value.clone()).ok())
            .unwrap_or_default(),
        linked_knowledge_ids,
        linked_knowledge_summaries,
        status: details
            .get("status")
            .and_then(Value::as_str)
            .map(ToString::to_string)
            .unwrap_or_else(|| {
                if record.user_verified {
                    "confirmed".to_string()
                } else {
                    "candidate".to_string()
                }
            }),
        created_at: record.created_at,
        created_at_ms: record.created_at_ms,
        updated_at: record.updated_at,
        updated_at_ms: record.updated_at_ms,
    }
}

fn resolve_linked_knowledge_summaries(
    storage: &StorageManager,
    linked_knowledge_ids: &[String],
) -> Vec<BakeLinkedKnowledgeSummaryPayload> {
    linked_knowledge_ids
        .iter()
        .filter_map(|id| {
            let parsed_id = id.parse::<i64>().ok()?;
            let entry = storage.get_timeline_entry(parsed_id).ok().flatten()?;
            Some(BakeLinkedKnowledgeSummaryPayload {
                id: id.clone(),
                summary: entry.summary,
            })
        })
        .collect()
}

fn is_high_value_candidate(candidate: &BakeMemorySourceRecord) -> bool {
    let record = &candidate.timeline;
    if record.is_self_generated {
        return false;
    }
    // 文档是否进入 bake 由“文档身份 + 实质正文”确定，不再依赖 importance。
    if is_substantive_document_candidate(candidate) {
        return true;
    }
    if record.importance >= 4 || record.user_verified {
        return true;
    }

    // 文档类候选（用户查看过某份文档）：直接通过，不要求 strong_evidence。
    // 是否真有可提炼的文档内容由 design 提炼阶段判断。
    if record.history_view {
        return true;
    }

    let strong_evidence = matches!(
        record.evidence_strength.as_deref(),
        Some("high") | Some("medium")
    );
    let preferred_activity = matches!(
        record.activity_type.as_deref(),
        Some("coding") | Some("reading") | Some("reviewing_history") | Some("document_reference")
    );
    let preferred_origin = matches!(
        record.content_origin.as_deref(),
        Some("historical_content") | Some("live_interaction")
    );

    strong_evidence && (preferred_activity || preferred_origin)
}

fn is_substantive_document_candidate(candidate: &BakeMemorySourceRecord) -> bool {
    document_evidence(candidate).allows_auto_create
}

fn document_evidence(candidate: &BakeMemorySourceRecord) -> BakeDocumentEvidencePayload {
    const MIN_DOCUMENT_CHARS: usize = 200;

    let app_name = candidate.capture_app_name.as_deref().unwrap_or_default();
    let source_surface = classify_source_surface(app_name);
    let has_document_url = candidate
        .capture_url
        .as_deref()
        .is_some_and(looks_like_document_url);
    let has_document_page_title = candidate
        .preferred_source_title
        .as_deref()
        .is_some_and(looks_like_document_title)
        || candidate
            .capture_webpage_title
            .as_deref()
            .is_some_and(looks_like_document_title)
        || candidate
            .capture_win_title
            .as_deref()
            .is_some_and(looks_like_document_title);
    let aggregated_body_char_count = candidate
        .url_aggregated_text
        .as_deref()
        .filter(|text| !text.trim().is_empty())
        .map(non_whitespace_char_count)
        .unwrap_or(0);
    let capture_body_char_count = [
        candidate.capture_ax_text.as_deref(),
        candidate.capture_ocr_text.as_deref(),
        candidate.capture_input_text.as_deref(),
        candidate.capture_audio_text.as_deref(),
    ]
    .into_iter()
    .flatten()
    .map(non_whitespace_char_count)
    .sum();
    let body_char_count = aggregated_body_char_count.max(capture_body_char_count);
    let has_substantive_document_body = body_char_count >= MIN_DOCUMENT_CHARS;
    let has_meaningful_native_title = [
        candidate.preferred_source_title.as_deref(),
        candidate.capture_webpage_title.as_deref(),
        candidate.capture_win_title.as_deref(),
    ]
    .into_iter()
    .flatten()
    .any(|title| canonical_document_source_title(title, Some(app_name)).is_some());

    let is_code_editor = is_code_editor_app(app_name);
    let kind = if !has_substantive_document_body || source_surface == BakeSourceSurface::Chat {
        BakeDocumentEvidenceKind::Insufficient
    } else if has_document_url {
        BakeDocumentEvidenceKind::DocumentUrl
    } else if source_surface == BakeSourceSurface::Browser && has_document_page_title {
        BakeDocumentEvidenceKind::BrowserDocument
    } else if source_surface == BakeSourceSurface::DocumentEditor
        && has_meaningful_native_title
        && (!is_code_editor || has_document_page_title)
    {
        BakeDocumentEvidenceKind::NativeDocument
    } else {
        BakeDocumentEvidenceKind::Insufficient
    };

    BakeDocumentEvidencePayload {
        kind,
        source_surface,
        has_document_url,
        has_document_page_title,
        has_substantive_document_body,
        allows_auto_create: kind != BakeDocumentEvidenceKind::Insufficient,
    }
}

fn non_whitespace_char_count(value: &str) -> usize {
    value.chars().filter(|ch| !ch.is_whitespace()).count()
}

fn classify_source_surface(app_name: &str) -> BakeSourceSurface {
    let normalized = app_name.trim().to_lowercase();
    if [
        "kim",
        "kem",
        "微信",
        "wechat",
        "slack",
        "teams",
        "microsoft teams",
        "钉钉",
        "dingtalk",
        "飞书",
        "feishu",
        "lark",
    ]
    .iter()
    .any(|marker| normalized == *marker || normalized.contains(marker))
    {
        return BakeSourceSurface::Chat;
    }
    if [
        "chrome",
        "safari",
        "arc",
        "edge",
        "firefox",
        "chatgpt atlas",
    ]
    .iter()
    .any(|marker| normalized.contains(marker))
    {
        return BakeSourceSurface::Browser;
    }
    if [
        "microsoft word",
        "word",
        "pages",
        "wps",
        "libreoffice writer",
        "obsidian",
        "typora",
        "cursor",
        "visual studio code",
        "code",
    ]
    .iter()
    .any(|marker| normalized == *marker || normalized.contains(marker))
    {
        return BakeSourceSurface::DocumentEditor;
    }
    BakeSourceSurface::Other
}

fn is_code_editor_app(app_name: &str) -> bool {
    let normalized = app_name.trim().to_lowercase();
    ["cursor", "visual studio code", "code", "xcode"]
        .iter()
        .any(|marker| normalized == *marker || normalized.contains(marker))
}

fn looks_like_document_title(title: &str) -> bool {
    let lowered = title.trim().to_lowercase();
    [
        "云文档",
        "在线文档",
        "google docs",
        "google 文档",
        "飞书文档",
        "语雀",
        "notion",
        "confluence",
        "石墨文档",
        ".doc",
        ".docx",
        ".pages",
        ".md",
    ]
    .iter()
    .any(|marker| lowered.contains(marker))
}

fn substantive_document_url(candidate: &BakeMemorySourceRecord) -> Option<String> {
    if !is_substantive_document_candidate(candidate) {
        return None;
    }
    candidate
        .capture_url
        .as_deref()
        .filter(|url| looks_like_document_url(url))
        .map(normalize_doc_url)
        .filter(|url| !url.is_empty())
}

fn reserve_document_task(
    candidate: &BakeMemorySourceRecord,
    queued_document_urls: &mut std::collections::HashSet<String>,
) -> bool {
    substantive_document_url(candidate)
        .map(|url| queued_document_urls.insert(url))
        .unwrap_or(true)
}

fn looks_like_document_url(url: &str) -> bool {
    let lowered = url.trim().to_lowercase();
    [
        "/docs/",
        "docs.google",
        "/document/",
        "yuque.com",
        "feishu.cn/docx",
        "feishu.cn/wiki",
        "notion.so",
        "confluence",
        "/wiki/",
        "shimo.im",
        "/d/home/",
        "/s/home/",
        "/k/home/",
    ]
    .iter()
    .any(|marker| lowered.contains(marker))
}

fn build_inventory_trend(
    memory_created_at: &[i64],
    data_created_at: &[i64],
    knowledge_created_at: &[i64],
    document_created_at: &[i64],
    sop_created_at: &[i64],
) -> Vec<BakeInventoryTrendBucketPayload> {
    // 运营台趋势图支持 7/30/90 天范围：最近 90 天必须按天分桶，
    // 否则多日桶会被 UI 整体归到桶起始日，造成单日吞吐失真（
    // 如把一周产出堆到某一天，其余天显示为 0）。
    const DAILY_WINDOW_DAYS: i64 = 90;

    let timestamps = memory_created_at
        .iter()
        .chain(data_created_at.iter())
        .chain(knowledge_created_at.iter())
        .chain(document_created_at.iter())
        .chain(sop_created_at.iter())
        .copied()
        .filter(|ts| *ts > 0)
        .collect::<Vec<_>>();

    let Some(min_ts) = timestamps.iter().min().copied() else {
        return Vec::new();
    };
    let Some(max_ts) = timestamps.iter().max().copied() else {
        return Vec::new();
    };

    // 各资产列表的日期筛选按本地自然日计算，这里必须使用同一天界。
    // 之前直接用 Unix 毫秒整除会以 UTC 0 点分桶，例如上海时间 00:00-08:00
    // 生成的产物会被记忆页算到前一天。
    let Some(start_day) = local_day_start_ms(min_ts) else {
        return Vec::new();
    };
    let Some(end_day) = local_day_start_ms(max_ts) else {
        return Vec::new();
    };
    let daily_start_day = add_local_days(end_day, -(DAILY_WINDOW_DAYS - 1)).max(start_day);

    let make_bucket = |start_ts: i64, end_exclusive_ts: i64| BakeInventoryTrendBucketPayload {
        label: format_trend_bucket_label(start_ts, end_exclusive_ts),
        start_ts,
        end_ts: end_exclusive_ts - 1,
        memory_count: count_records_in_bucket(
            memory_created_at.iter().copied(),
            start_ts,
            end_exclusive_ts,
        ),
        data_count: count_records_in_bucket(
            data_created_at.iter().copied(),
            start_ts,
            end_exclusive_ts,
        ),
        knowledge_count: count_records_in_bucket(
            knowledge_created_at.iter().copied(),
            start_ts,
            end_exclusive_ts,
        ),
        template_count: count_records_in_bucket(
            document_created_at.iter().copied(),
            start_ts,
            end_exclusive_ts,
        ),
        sop_count: count_records_in_bucket(
            sop_created_at.iter().copied(),
            start_ts,
            end_exclusive_ts,
        ),
    };

    let mut buckets: Vec<BakeInventoryTrendBucketPayload> = Vec::new();
    // 早期数据按周聚合（桶边界裁切到每日窗口起点，避免与每日桶重复计数）。
    let mut cursor = start_day;
    while cursor < daily_start_day {
        let bucket_end = add_local_days(cursor, 7).min(daily_start_day);
        buckets.push(make_bucket(cursor, bucket_end));
        cursor = bucket_end;
    }
    // 最近窗口按本地自然日分桶。
    let mut day = daily_start_day;
    while day <= end_day {
        let next_day = add_local_days(day, 1);
        buckets.push(make_bucket(day, next_day));
        day = next_day;
    }
    buckets
}

fn local_day_start_ms(timestamp_ms: i64) -> Option<i64> {
    let local = chrono::DateTime::<Utc>::from_timestamp_millis(timestamp_ms)?.with_timezone(&Local);
    let midnight = local.date_naive().and_hms_opt(0, 0, 0)?;
    Local
        .from_local_datetime(&midnight)
        .earliest()
        .map(|value| value.timestamp_millis())
}

fn add_local_days(day_start_ms: i64, days: i64) -> i64 {
    let Some(local) = chrono::DateTime::<Utc>::from_timestamp_millis(day_start_ms)
        .map(|value| value.with_timezone(&Local))
    else {
        return day_start_ms;
    };
    let Some(date) = local
        .date_naive()
        .checked_add_signed(chrono::Duration::days(days))
    else {
        return day_start_ms;
    };
    let Some(midnight) = date.and_hms_opt(0, 0, 0) else {
        return day_start_ms;
    };
    Local
        .from_local_datetime(&midnight)
        .earliest()
        .map(|value| value.timestamp_millis())
        .unwrap_or(day_start_ms)
}

fn count_records_in_bucket<I>(timestamps: I, start_ts: i64, end_ts: i64) -> i64
where
    I: Iterator<Item = i64>,
{
    timestamps
        .filter(|ts| *ts > 0 && *ts >= start_ts && *ts < end_ts)
        .count() as i64
}

fn format_trend_bucket_label(start_ts: i64, end_exclusive_ts: i64) -> String {
    let Some(start) = chrono::DateTime::<Utc>::from_timestamp_millis(start_ts)
        .map(|value| value.with_timezone(&Local))
    else {
        return "未知".to_string();
    };
    let Some(end) = chrono::DateTime::<Utc>::from_timestamp_millis(end_exclusive_ts - 1)
        .map(|value| value.with_timezone(&Local))
    else {
        return start.format("%Y-%m-%d").to_string();
    };
    if start.date_naive() == end.date_naive() {
        return start.format("%Y-%m-%d").to_string();
    }
    format!("{}-{}", start.format("%Y-%m-%d"), end.format("%Y-%m-%d"))
}

fn parse_details(value: Option<&str>) -> Value {
    value
        .and_then(|text| serde_json::from_str::<Value>(text).ok())
        .unwrap_or_else(|| json!({}))
}

fn is_current_bake_entry(record: &TimelineRecord) -> bool {
    let details = parse_details(record.details.as_deref());
    !is_legacy_bake_entry_details(&details)
}

fn is_legacy_bake_entry_details(details: &Value) -> bool {
    details.get("creation_mode").and_then(Value::as_str) == Some("auto")
        && details.get("generation_version").and_then(Value::as_str)
            == Some(BAKE_GENERATION_VERSION)
}

fn is_current_bake_document(record: &BakeDocumentRecord) -> bool {
    !is_legacy_bake_document(record)
}

fn is_legacy_bake_document(record: &BakeDocumentRecord) -> bool {
    record.creation_mode == "auto"
        && record.generation_version.as_deref() == Some(BAKE_GENERATION_VERSION)
}

fn matches_document_bucket(record: &BakeDocumentRecord, bucket: Option<BakeBucket>) -> bool {
    match bucket {
        None => record.review_status != "ignored",
        Some(BakeBucket::Pending) => false,
        Some(BakeBucket::Extracted) => record.review_status != "ignored",
    }
}

fn matches_entry_bucket(record: &TimelineRecord, bucket: Option<BakeBucket>) -> bool {
    let status = extract_status(record);
    match bucket {
        None => status != "ignored",
        Some(BakeBucket::Pending) => false,
        Some(BakeBucket::Extracted) => status != "ignored",
    }
}

fn extract_status_from_details(details: &Value, user_verified: bool) -> String {
    details
        .get("status")
        .and_then(Value::as_str)
        .map(ToString::to_string)
        .unwrap_or_else(|| {
            if user_verified {
                "confirmed".to_string()
            } else {
                "candidate".to_string()
            }
        })
}

fn infer_semantic_type_label(record: &CaptureRecord) -> String {
    if record
        .input_text
        .as_deref()
        .is_some_and(has_meaningful_text)
    {
        return "输入片段".to_string();
    }
    if record
        .audio_text
        .as_deref()
        .is_some_and(has_meaningful_text)
    {
        return "语音片段".to_string();
    }
    if record.screenshot_path.is_some()
        || record.ocr_text.as_deref().is_some_and(has_meaningful_text)
    {
        return "截图片段".to_string();
    }
    if record.ax_text.as_deref().is_some_and(has_meaningful_text)
        || record.ax_focused_role.is_some()
    {
        return "界面片段".to_string();
    }
    friendly_event_type_label(&record.event_type).to_string()
}

fn friendly_raw_type_label(event_type: &str, record: &CaptureRecord) -> String {
    if record
        .input_text
        .as_deref()
        .is_some_and(has_meaningful_text)
    {
        return "原始模态：输入".to_string();
    }
    if record
        .audio_text
        .as_deref()
        .is_some_and(has_meaningful_text)
    {
        return "原始模态：音频".to_string();
    }
    if record.ocr_text.as_deref().is_some_and(has_meaningful_text)
        || record.screenshot_path.is_some()
    {
        return "原始模态：OCR / 截图".to_string();
    }
    if record.ax_text.as_deref().is_some_and(has_meaningful_text)
        || record.ax_focused_role.is_some()
    {
        return "原始模态：AX / UI".to_string();
    }
    format!("原始事件：{}", friendly_event_type_label(event_type))
}

fn friendly_event_type_label(event_type: &str) -> &'static str {
    match event_type {
        "app_switch" => "应用切换",
        "browser_navigation" => "网页切换",
        "mouse_click" => "鼠标点击",
        "scroll" => "滚动",
        "key_pause" => "键入停顿",
        "manual" => "手动记录",
        "auto" => "自动采集",
        _ => "其他片段",
    }
}

fn has_meaningful_text(value: &str) -> bool {
    !value.trim().is_empty()
}

fn deserialize_string_vec_mixed<'de, D>(deserializer: D) -> Result<Vec<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<Value>::deserialize(deserializer)?.unwrap_or(Value::Null);
    Ok(mixed_value_to_strings(value))
}

fn deserialize_optional_string_mixed<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<Value>::deserialize(deserializer)?.unwrap_or(Value::Null);
    Ok(mixed_value_to_string(value))
}

fn deserialize_document_sections_mixed<'de, D>(
    deserializer: D,
) -> Result<Vec<DocumentSectionPayload>, D::Error>
where
    D: Deserializer<'de>,
{
    let value = Option::<Value>::deserialize(deserializer)?.unwrap_or(Value::Null);
    let values = match value {
        Value::Array(values) => values,
        Value::Null => Vec::new(),
        value => vec![value],
    };
    Ok(values
        .into_iter()
        .filter_map(|value| match value {
            Value::Object(mut object) => {
                let title = ["title", "name", "heading"]
                    .into_iter()
                    .find_map(|key| object.remove(key).and_then(mixed_value_to_string))
                    .unwrap_or_default();
                if title.trim().is_empty() {
                    return None;
                }
                let keywords = object
                    .remove("keywords")
                    .map(mixed_value_to_strings)
                    .unwrap_or_default();
                let notes = ["notes", "content", "description"]
                    .into_iter()
                    .find_map(|key| object.remove(key).and_then(mixed_value_to_string));
                Some(DocumentSectionPayload {
                    title,
                    keywords,
                    notes,
                })
            }
            Value::String(title)
                if !title.trim().is_empty()
                    && !title.contains("\":[")
                    && !title.contains("\": [") =>
            {
                Some(DocumentSectionPayload {
                    title,
                    keywords: Vec::new(),
                    notes: None,
                })
            }
            _ => None,
        })
        .collect())
}

fn mixed_value_to_strings(value: Value) -> Vec<String> {
    match value {
        Value::Array(values) => values
            .into_iter()
            .filter_map(mixed_value_to_string)
            .filter(|value| !value.trim().is_empty())
            .collect(),
        Value::Null => Vec::new(),
        value => mixed_value_to_string(value).into_iter().collect(),
    }
}

fn mixed_value_to_string(value: Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(value) => Some(value),
        Value::Number(value) => Some(value.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        Value::Array(values) => {
            let values = values
                .into_iter()
                .filter_map(mixed_value_to_string)
                .filter(|value| !value.trim().is_empty())
                .collect::<Vec<_>>();
            (!values.is_empty()).then(|| values.join("；"))
        }
        Value::Object(mut object) => {
            for key in ["description", "content", "step", "title", "name", "value"] {
                if let Some(value) = object.remove(key).and_then(mixed_value_to_string) {
                    return Some(value);
                }
            }
            serde_json::to_string(&object).ok()
        }
    }
}

fn parse_json_vec_string(value: &str) -> Vec<String> {
    serde_json::from_str::<Vec<String>>(value).unwrap_or_default()
}

fn parse_json_vec_string_lossy(value: &str) -> Vec<String> {
    serde_json::from_str::<Vec<Value>>(value)
        .map(|values| {
            values
                .into_iter()
                .filter_map(|value| match value {
                    Value::String(item) => Some(item),
                    Value::Number(item) => Some(item.to_string()),
                    Value::Bool(item) => Some(item.to_string()),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default()
}

fn parse_optional_json_vec_string(value: &Option<String>) -> Vec<String> {
    value
        .as_deref()
        .map(parse_json_vec_string_lossy)
        .unwrap_or_default()
}

fn merge_string_lists(mut base: Vec<String>, extra: &[String]) -> Vec<String> {
    for item in extra {
        let item = item.trim();
        if !item.is_empty() && !base.iter().any(|value| value == item) {
            base.push(item.to_string());
        }
    }
    base
}

fn option_f64_json(value: Option<f64>) -> Value {
    value.map_or(Value::Null, Value::from)
}

fn option_string_json(value: Option<&str>) -> Value {
    value
        .map(|item| Value::String(item.to_string()))
        .unwrap_or(Value::Null)
}

fn merge_optional_text(existing: Option<&str>, incoming: Option<&str>) -> Option<String> {
    let existing = existing.map(str::trim).filter(|value| !value.is_empty());
    let incoming = incoming.map(str::trim).filter(|value| !value.is_empty());
    match (existing, incoming) {
        (Some(old), Some(new)) if old == new || old.contains(new) => Some(old.to_string()),
        (Some(old), Some(new)) if new.contains(old) => Some(new.to_string()),
        (Some(old), Some(new)) => Some(format!("{old}\n\n---\n\n{new}")),
        (Some(old), None) => Some(old.to_string()),
        (None, Some(new)) => Some(new.to_string()),
        (None, None) => None,
    }
}

fn knowledge_title_from_payload(payload: &BakeKnowledgeArtifactPayload) -> String {
    payload
        .overview
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| payload.summary.trim())
        .to_string()
}

fn knowledge_summary_from_payload(payload: &BakeKnowledgeArtifactPayload) -> String {
    payload
        .overview
        .clone()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| payload.summary.clone())
}

fn parse_json_value<T>(value: &str) -> Vec<T>
where
    T: for<'de> Deserialize<'de>,
{
    serde_json::from_str::<Vec<T>>(value).unwrap_or_default()
}

fn to_json_string<T: Serialize>(value: &T) -> Result<String, ApiError> {
    serde_json::to_string(value)
        .map_err(|err| ApiError::Internal(format!("序列化 bake 数据失败: {err}")))
}

fn infer_suggested_action(tags: &[String]) -> Option<String> {
    if tags
        .iter()
        .any(|tag| tag.contains("SOP") || tag.contains("流程"))
    {
        Some("sop".to_string())
    } else if tags
        .iter()
        .any(|tag| tag.contains("方案") || tag.contains("设计") || tag.contains("架构"))
    {
        Some("design".to_string())
    } else {
        Some("knowledge".to_string())
    }
}

fn infer_confidence(importance: i64, occurrence_count: Option<i64>) -> String {
    let occurrences = occurrence_count.unwrap_or(0);
    if importance >= 4 || occurrences >= 3 {
        "high".to_string()
    } else if importance >= 3 || occurrences >= 1 {
        "medium".to_string()
    } else {
        "low".to_string()
    }
}

fn extract_status(entry: &TimelineRecord) -> String {
    let details = parse_details(entry.details.as_deref());
    extract_status_from_details(&details, entry.user_verified)
}

fn first_or_default(values: &[String], default: &str) -> String {
    values
        .first()
        .cloned()
        .unwrap_or_else(|| default.to_string())
}

fn default_style_config() -> BakeStyleConfig {
    BakeStyleConfig {
        preferred_phrases: vec![
            "整体看".to_string(),
            "这里建议".to_string(),
            "当前主要问题是".to_string(),
        ],
        replacement_rules: vec![
            ReplacementRulePayload {
                from: "综上所述".to_string(),
                to: "整体看".to_string(),
            },
            ReplacementRulePayload {
                from: "进一步优化".to_string(),
                to: "继续改进".to_string(),
            },
        ],
        style_samples: vec![
            "整体看，这次改动优先解决主链路稳定性问题。".to_string(),
            "这里建议先把页面骨架搭起来，再逐步接真接口。".to_string(),
        ],
        apply_to_creation: true,
        apply_to_template_editing: true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_inventory_trend_counts_data_sources_by_creation_time() {
        const DAY_MS: i64 = 86_400_000;
        let first_day = (1_710_000_000_000 / DAY_MS) * DAY_MS;
        let data_sources = vec![first_day + 1_000, first_day + DAY_MS + 1_000];

        let buckets = build_inventory_trend(&[], &data_sources, &[], &[], &[]);

        assert_eq!(
            buckets.iter().map(|bucket| bucket.data_count).sum::<i64>(),
            2
        );
        assert!(buckets.iter().all(|bucket| {
            bucket.memory_count == 0
                && bucket.knowledge_count == 0
                && bucket.template_count == 0
                && bucket.sop_count == 0
        }));
    }

    #[test]
    fn test_inventory_trend_uses_local_midnight_like_monitor_today() {
        let local_midnight = Local
            .with_ymd_and_hms(2026, 8, 8, 0, 0, 0)
            .earliest()
            .unwrap()
            .timestamp_millis();
        let data_sources = vec![local_midnight - 1, local_midnight + 1];

        let buckets = build_inventory_trend(&[], &data_sources, &[], &[], &[]);

        let previous_day = buckets
            .iter()
            .find(|bucket| bucket.label == "2026-08-07")
            .unwrap();
        let current_day = buckets
            .iter()
            .find(|bucket| bucket.label == "2026-08-08")
            .unwrap();
        assert_eq!(previous_day.data_count, 1);
        assert_eq!(current_day.data_count, 1);
        assert_eq!(current_day.start_ts, local_midnight);
    }

    #[test]
    fn test_inventory_trend_uses_daily_buckets_for_recent_window() {
        let newest_day = Local
            .with_ymd_and_hms(2027, 1, 15, 0, 0, 0)
            .earliest()
            .unwrap()
            .timestamp_millis();
        let data_sources = vec![
            newest_day + 1_000,
            add_local_days(newest_day, -1) + 1_000,
            // 90 天窗口之外的早期数据落入周聚合桶，不与每日桶重叠。
            add_local_days(newest_day, -120) + 1_000,
        ];

        let buckets = build_inventory_trend(&[], &data_sources, &[], &[], &[]);

        assert_eq!(
            buckets.iter().map(|bucket| bucket.data_count).sum::<i64>(),
            3
        );
        // 最近两天各自独立成桶，不再被多日桶合并到起始日。
        let recent: Vec<&BakeInventoryTrendBucketPayload> = buckets
            .iter()
            .filter(|bucket| bucket.start_ts >= add_local_days(newest_day, -1))
            .collect();
        assert_eq!(recent.len(), 2);
        assert!(recent.iter().all(|bucket| bucket.data_count == 1));
        assert!(recent
            .iter()
            .all(|bucket| bucket.end_ts + 1 == add_local_days(bucket.start_ts, 1)));
        // 早期周聚合桶裁切在每日窗口起点之前，不与每日桶重叠计数。
        let daily_start_day = add_local_days(newest_day, -89);
        let early: Vec<&BakeInventoryTrendBucketPayload> = buckets
            .iter()
            .filter(|bucket| bucket.start_ts < daily_start_day)
            .collect();
        assert!(!early.is_empty());
        assert!(early.iter().all(|bucket| bucket.end_ts < daily_start_day));
        assert_eq!(early.iter().map(|bucket| bucket.data_count).sum::<i64>(), 1);
    }

    #[test]
    fn test_inventory_trend_counts_each_new_asset_once_on_creation_day() {
        let creation_day = Local
            .with_ymd_and_hms(2026, 8, 10, 0, 0, 0)
            .earliest()
            .unwrap()
            .timestamp_millis();
        let documents = vec![creation_day + 17 * 60 * 60 * 1000];

        let buckets = build_inventory_trend(&[], &[], &[], &documents, &[]);

        let today = buckets
            .iter()
            .find(|bucket| bucket.label == "2026-08-10")
            .unwrap();
        assert_eq!(today.template_count, 1);
        assert_eq!(today.knowledge_count, 0);
        assert_eq!(today.sop_count, 0);
    }

    #[test]
    fn test_document_date_filter_uses_creation_time_instead_of_update_time() {
        let service = make_service();
        let document_id = service
            .storage
            .insert_bake_document(&NewBakeDocument::with_defaults(
                "历史文档".to_string(),
                "技术文档".to_string(),
            ))
            .unwrap();
        let today = Local
            .with_ymd_and_hms(2026, 8, 11, 0, 0, 0)
            .earliest()
            .unwrap()
            .timestamp_millis();
        let yesterday = add_local_days(today, -1);
        let updated_at = today + 35 * 60 * 1000;
        service
            .storage
            .with_conn(|conn| {
                conn.execute(
                    "UPDATE bake_documents SET created_at = ?1, updated_at = ?2 WHERE id = ?3",
                    rusqlite::params![yesterday + 1_000, updated_at, document_id],
                )?;
                Ok(())
            })
            .unwrap();

        let today_page = service
            .list_documents_paginated(BakeListFilter {
                from_ts: Some(today),
                to_ts: Some(add_local_days(today, 1) - 1),
                limit: 20,
                ..BakeListFilter::default()
            })
            .unwrap();
        assert_eq!(today_page.total, 0);

        let yesterday_page = service
            .list_documents_paginated(BakeListFilter {
                from_ts: Some(yesterday),
                to_ts: Some(today - 1),
                limit: 20,
                ..BakeListFilter::default()
            })
            .unwrap();
        assert_eq!(yesterday_page.total, 1);
        assert_eq!(yesterday_page.items[0].updated_at_ms, updated_at);
    }

    #[test]
    fn test_candidate_failures_use_bounded_retry_classification() {
        for code in [
            "INFERENCE_PREEMPTED",
            "MODEL_RATE_LIMITED",
            "MODEL_UNAVAILABLE",
            "SIDECAR_UNAVAILABLE",
        ] {
            let error = ApiError::Upstream {
                status: StatusCode::SERVICE_UNAVAILABLE,
                code,
                message: "稍后重试".to_string(),
            };
            assert!(is_untracked_transient_bake_error(&error));
            assert!(is_transient_bake_error(&error));
        }
        for code in [
            "BAKE_MODEL_UPSTREAM_ERROR",
            "BAKE_INTERNAL_ERROR",
            "BAKE_UNCLASSIFIED_UPSTREAM_ERROR",
        ] {
            let error = ApiError::Upstream {
                status: StatusCode::BAD_GATEWAY,
                code,
                message: "候选级错误".to_string(),
            };
            assert!(!is_untracked_transient_bake_error(&error));
            assert!(is_retryable_bake_candidate_error(&error));
            assert!(is_transient_bake_error(&error));
        }
        let timeout = ApiError::Upstream {
            status: StatusCode::GATEWAY_TIMEOUT,
            code: "INFERENCE_TIMEOUT",
            message: "任务已取消".to_string(),
        };
        assert!(!is_untracked_transient_bake_error(&timeout));
        assert!(is_retryable_bake_candidate_error(&timeout));
        assert!(is_transient_bake_error(&timeout));
        assert!(!is_transient_bake_error(&ApiError::Internal(
            "永久 payload 错误".to_string(),
        )));
        assert_eq!(MAX_BAKE_RETRY_FAILURES, 3);
    }

    #[test]
    fn test_sidecar_error_contract_does_not_infer_service_scope_from_5xx() {
        let cases = [
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({
                    "code": "BAKE_INTERNAL_ERROR",
                    "scope": "candidate",
                    "retryable": true
                })
                .to_string(),
                "BAKE_INTERNAL_ERROR",
                false,
                true,
            ),
            (
                StatusCode::BAD_GATEWAY,
                json!({
                    "code": "BAKE_MODEL_UPSTREAM_ERROR",
                    "scope": "candidate",
                    "retryable": true
                })
                .to_string(),
                "BAKE_MODEL_UPSTREAM_ERROR",
                false,
                true,
            ),
            (
                StatusCode::SERVICE_UNAVAILABLE,
                json!({
                    "code": "MODEL_UNAVAILABLE",
                    "scope": "service",
                    "retryable": true
                })
                .to_string(),
                "MODEL_UNAVAILABLE",
                true,
                false,
            ),
            (
                StatusCode::BAD_GATEWAY,
                r#"{"error":"unstructured failure"}"#.to_string(),
                "BAKE_UNCLASSIFIED_UPSTREAM_ERROR",
                false,
                true,
            ),
            (
                StatusCode::SERVICE_UNAVAILABLE,
                json!({
                    "code": "MODEL_UNAVAILABLE",
                    "scope": "candidate",
                    "retryable": true
                })
                .to_string(),
                "BAKE_UNCLASSIFIED_UPSTREAM_ERROR",
                false,
                true,
            ),
            (
                StatusCode::TOO_MANY_REQUESTS,
                r#"{"error":"unstructured rate limit"}"#.to_string(),
                "BAKE_UNCLASSIFIED_UPSTREAM_ERROR",
                false,
                true,
            ),
        ];

        for (status, body, expected_code, untracked, candidate_retry) in cases {
            let mapped = map_sidecar_error(status, body, "bake 提炼服务");
            assert_eq!(mapped.code, expected_code);
            let error = ApiError::Upstream {
                status: mapped.status,
                code: mapped.code,
                message: mapped.message,
            };
            assert_eq!(is_untracked_transient_bake_error(&error), untracked);
            assert_eq!(is_retryable_bake_candidate_error(&error), candidate_retry);
        }
    }

    #[test]
    fn test_sidecar_error_message_does_not_expose_raw_provider_body() {
        let mapped = map_sidecar_error(
            StatusCode::INTERNAL_SERVER_ERROR,
            r#"{"error":"provider-model secret response"}"#.to_string(),
            "bake 提炼服务",
        );
        assert!(!mapped.message.contains("provider-model"));
        assert!(!mapped.message.contains("secret response"));
    }

    #[test]
    fn test_gateway_timeout_is_identified_as_terminal_candidate_timeout() {
        assert!(is_bake_candidate_timeout(&ApiError::Upstream {
            status: StatusCode::GATEWAY_TIMEOUT,
            code: "GATEWAY_TIMEOUT",
            message: "bake 提炼超时".to_string(),
        }));
        assert!(!is_bake_candidate_timeout(&ApiError::Upstream {
            status: StatusCode::SERVICE_UNAVAILABLE,
            code: "INFERENCE_PREEMPTED",
            message: "在线任务抢占".to_string(),
        }));
    }

    #[test]
    fn test_structured_truncated_output_uses_bounded_retry() {
        let mapped = map_sidecar_error(
            StatusCode::UNPROCESSABLE_ENTITY,
            json!({
                "code": "BAKE_OUTPUT_TRUNCATED",
                "error": "bake bundle output invalid: truncated_json",
                "retryable": true,
                "scope": "candidate",
            })
            .to_string(),
            "bake 提炼服务",
        );
        assert_eq!(mapped.status, StatusCode::UNPROCESSABLE_ENTITY);
        assert_eq!(mapped.code, "BAKE_OUTPUT_TRUNCATED");
        let error = ApiError::Upstream {
            status: mapped.status,
            code: mapped.code,
            message: mapped.message,
        };
        assert!(is_retryable_bake_candidate_error(&error));
        assert!(is_transient_bake_error(&error));
    }

    #[test]
    fn test_merge_document_response_allows_missing_title() {
        let payload: BakeMergeDocumentResponse =
            serde_json::from_value(json!({"no_change": true})).unwrap();
        assert_eq!(payload.title, None);
        assert!(payload.no_change);
    }

    #[test]
    fn test_bake_payloads_tolerate_model_scalar_and_object_variants() {
        let sop: BakeSopArtifactPayload = serde_json::from_value(json!({
            "summary": "排查流程",
            "steps": [{"description": "检查监控"}, "验证结果"],
            "linked_knowledge_ids": [1, "2"],
            "confidence": 1.0
        }))
        .unwrap();
        assert_eq!(sop.steps, vec!["检查监控", "验证结果"]);
        assert_eq!(sop.linked_knowledge_ids, vec!["1", "2"]);
        assert_eq!(sop.confidence.as_deref(), Some("1.0"));

        let document: BakeDocumentArtifactPayload = serde_json::from_value(json!({
            "name": "技术文档",
            "tags": [1, "弹性"],
            "structure_sections": [
                {"heading": "背景", "keywords": ["潮汐特性"], "notes": 2},
                "实施方案",
                "style_phrases\": ["
            ]
        }))
        .unwrap();
        assert_eq!(document.tags, vec!["1", "弹性"]);
        assert_eq!(document.sections.len(), 2);
        assert_eq!(document.sections[0].title, "背景");
        assert_eq!(document.sections[0].keywords, vec!["潮汐特性".to_string()]);
        assert_eq!(document.sections[0].notes.as_deref(), Some("2"));
    }

    #[test]
    fn test_bake_payload_identity_falls_back_to_candidate() {
        let service = make_service();
        let capture_id = seed_capture(&service, 1_710_000_000_000, "Google Chrome", "候选文档标题");
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 4, 1);
        let candidate = make_candidate(&service, timeline_id);

        let knowledge = parse_bake_knowledge_payload(json!({}), &candidate).unwrap();
        let document = parse_bake_document_payload(json!({}), &candidate).unwrap();
        let sop = parse_bake_sop_payload(json!({}), &candidate).unwrap();

        assert_eq!(knowledge.summary, candidate.timeline.summary);
        assert_eq!(document.title, "知识来源");
        assert_eq!(sop.summary, candidate.timeline.summary);
    }

    #[test]
    fn test_document_merge_guard_rejects_truncated_rewrite() {
        let existing = format!("{}不可丢失的尾部", "A".repeat(3_000));

        assert!(!document_merge_preserves_existing(
            Some(existing.as_str()),
            &existing[..3_000],
        ));
        assert!(document_merge_preserves_existing(
            Some(existing.as_str()),
            &format!("{existing}\n\n新增章节"),
        ));
    }

    #[test]
    fn test_substantive_document_bypasses_importance_gate() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "弹性伸缩 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 2, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url =
            Some("https://docs.example.com/k/home/space/document-id?from=home#section".to_string());
        candidate.capture_ax_text = Some("文档正文".repeat(80));
        candidate.timeline.history_view = false;
        candidate.timeline.activity_type = None;
        candidate.timeline.content_origin = None;
        candidate.timeline.evidence_strength = None;

        assert!(is_high_value_candidate(&candidate));
        let evidence = document_evidence(&candidate);
        assert_eq!(evidence.kind, BakeDocumentEvidenceKind::DocumentUrl);
        assert!(evidence.allows_auto_create);
        assert_eq!(
            substantive_document_url(&candidate).as_deref(),
            Some("https://docs.example.com/k/home/space/document-id")
        );
    }

    #[test]
    fn test_document_without_substantive_body_does_not_bypass_importance_gate() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "弹性伸缩 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 2, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url =
            Some("https://docs.example.com/k/home/space/document-id".to_string());
        candidate.capture_ax_text = Some("文档标题".to_string());
        candidate.timeline.history_view = false;
        candidate.timeline.activity_type = None;
        candidate.timeline.content_origin = None;
        candidate.timeline.evidence_strength = None;

        assert!(!is_high_value_candidate(&candidate));
    }

    #[tokio::test]
    async fn test_chat_document_mentions_cannot_create_document_artifact() {
        let service = make_service();
        let capture_id = seed_capture(&service, 1_710_000_000_000, "Kim", "Kim");
        let timeline_id = seed_knowledge(&service, "会议", capture_id, 4, 2);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_app_name = Some("Kim".to_string());
        candidate.capture_win_title = Some("Kim".to_string());
        candidate.capture_ax_text = Some(
            "会议群聊天：你之前设计的那个剧本库文档看一下。[云文档] AIGC 剧本创作规范。".repeat(20),
        );
        candidate.capture_url = None;
        candidate.capture_webpage_title = None;
        candidate.timeline.history_view = false;
        candidate.timeline.activity_type = Some("meeting".to_string());
        candidate.timeline.content_origin = Some("live_interaction".to_string());

        assert!(is_high_value_candidate(&candidate));
        let evidence = document_evidence(&candidate);
        assert_eq!(evidence.kind, BakeDocumentEvidenceKind::Insufficient);
        assert_eq!(evidence.source_surface, BakeSourceSurface::Chat);
        assert!(evidence.has_substantive_document_body);
        assert!(!evidence.allows_auto_create);
        let payload = map_extract_candidate_payload(&candidate);
        assert_eq!(payload.timeline_category, "会议");
        assert_eq!(
            payload.document_evidence.kind,
            BakeDocumentEvidenceKind::Insufficient
        );
        assert!(!payload.document_evidence.allows_auto_create);

        let mut chat_with_document_link = candidate.clone();
        chat_with_document_link.capture_url =
            Some("https://docs.example.com/d/home/document-id".to_string());
        let linked_evidence = document_evidence(&chat_with_document_link);
        assert!(linked_evidence.has_document_url);
        assert_eq!(linked_evidence.kind, BakeDocumentEvidenceKind::Insufficient);
        assert!(!linked_evidence.allows_auto_create);

        let extraction = BakeArtifactExtraction {
            accepted: true,
            reason: None,
            payload: Some(json!({
                "name": "[云文档] AIGC 剧本创作规范（推测）",
                "full_content": "模型错误生成的文档正文",
                "match_score": 0.95,
                "match_level": "high",
                "review_status": "auto_created"
            })),
        };
        let mut existing_sources = std::collections::HashSet::new();
        let mut existing_urls = std::collections::HashSet::new();

        let result = service
            .persist_document_artifact(
                None,
                &candidate,
                &extraction,
                None,
                &mut existing_sources,
                &mut existing_urls,
            )
            .await
            .expect("聊天文档门禁执行失败");

        assert_eq!(result.discarded_count, 1);
        assert_eq!(result.document_created_count, 0);
        assert!(service.storage.list_bake_documents().unwrap().is_empty());
    }

    #[test]
    fn test_native_document_editor_without_url_has_document_evidence() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Microsoft Word",
            "季度复盘方案.docx",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 2, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_app_name = Some("Microsoft Word".to_string());
        candidate.capture_win_title = Some("季度复盘方案.docx".to_string());
        candidate.capture_ax_text = Some("季度复盘正文".repeat(80));
        candidate.capture_url = None;
        candidate.capture_webpage_title = None;

        let evidence = document_evidence(&candidate);

        assert_eq!(evidence.kind, BakeDocumentEvidenceKind::NativeDocument);
        assert_eq!(evidence.source_surface, BakeSourceSurface::DocumentEditor);
        assert!(evidence.allows_auto_create);
        assert!(is_high_value_candidate(&candidate));
    }

    #[test]
    fn test_document_source_title_is_stable_and_overrides_incremental_model_title() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Microsoft Word",
            "季度复盘方案.docx - Microsoft Word",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 4, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_app_name = Some("Microsoft Word".to_string());
        candidate.capture_win_title = Some("季度复盘方案.docx - Microsoft Word".to_string());
        candidate.capture_ax_text = Some("季度复盘正文".repeat(80));

        assert_eq!(
            document_source_title(&candidate).as_deref(),
            Some("季度复盘方案.docx")
        );
        let payload = parse_bake_document_payload(
            json!({
                "name": "文档增量：季度复盘方案",
                "full_content": "季度复盘正文"
            }),
            &candidate,
        )
        .unwrap();
        assert_eq!(payload.title, "季度复盘方案.docx");
    }

    #[test]
    fn test_reliable_later_source_title_repairs_existing_placeholder_title() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "商业化大模型例行压测介绍 - 云文档 - Google Chrome",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 4, 2);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_app_name = Some("Google Chrome".to_string());
        candidate.capture_win_title =
            Some("商业化大模型例行压测介绍 - 云文档 - Google Chrome".to_string());
        candidate.capture_webpage_title = Some("知识库".to_string());
        candidate.preferred_source_title = Some("商业化大模型例行压测介绍 - 云文档".to_string());
        candidate.capture_url =
            Some("https://docs.example.com/k/home/space/document-id".to_string());
        candidate.capture_ax_text = Some("压测文档正文".repeat(80));

        let payload = parse_bake_document_payload(
            json!({
                "name": "知识库",
                "full_content": "压测文档正文"
            }),
            &candidate,
        )
        .unwrap();
        let mut document = build_bake_document(
            &candidate,
            &payload,
            "auto_created",
            &[capture_id.to_string()],
            &[],
        )
        .unwrap();
        assert_eq!(document.linked_knowledge_ids, "[]");
        document.title = "知识库".to_string();
        document.source_win_title = Some("知识库".to_string());
        let document_id = service.storage.insert_bake_document(&document).unwrap();
        let existing = service
            .storage
            .get_bake_document(document_id)
            .unwrap()
            .unwrap();

        let (updated, changed) = service
            .document_with_merged_source_metadata(&candidate, &existing)
            .unwrap();

        assert!(changed);
        assert_eq!(updated.title, "商业化大模型例行压测介绍 - 云文档");
        assert_eq!(
            updated.source_win_title.as_deref(),
            Some("商业化大模型例行压测介绍 - 云文档")
        );
    }

    #[test]
    fn test_document_source_metadata_links_real_knowledge_id_not_timeline_id() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "商业体系 AI 能力盘点 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 4, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url = Some("https://docs.example.com/d/home/ai-inventory".to_string());
        candidate.capture_ax_text = Some("AI 能力盘点正文".repeat(80));

        let payload = parse_bake_document_payload(
            json!({
                "name": "商业体系 AI 能力盘点 - 云文档",
                "full_content": "AI 能力盘点正文"
            }),
            &candidate,
        )
        .unwrap();
        let document = build_bake_document(
            &candidate,
            &payload,
            "auto_created",
            &[capture_id.to_string()],
            &[],
        )
        .unwrap();
        let document_id = service.storage.insert_bake_document(&document).unwrap();
        service
            .storage
            .record_bake_artifact_source("knowledge", 777, timeline_id, None)
            .unwrap();
        let existing = service
            .storage
            .get_bake_document(document_id)
            .unwrap()
            .unwrap();

        let (updated, changed) = service
            .document_with_merged_source_metadata(&candidate, &existing)
            .unwrap();
        let linked = parse_json_vec_string(&updated.linked_knowledge_ids);

        assert!(changed);
        assert_eq!(linked, vec!["777"]);
        assert!(!linked.contains(&timeline_id.to_string()));
    }

    #[test]
    fn test_title_fallback_accepts_missing_url_but_rejects_different_urls() {
        assert!(document_urls_compatible_for_title_match(
            None,
            Some("https://docs.example.com/d/home/abc123?section=one"),
        ));
        assert!(document_urls_compatible_for_title_match(
            Some("https://docs.example.com/d/home/ABC123#one"),
            Some("http://docs.example.com/d/home/abc123?section=two"),
        ));
        assert!(!document_urls_compatible_for_title_match(
            Some("https://docs.example.com/d/home/abc123"),
            Some("https://docs.example.com/d/home/other456"),
        ));
    }

    #[test]
    fn test_document_source_fingerprint_ignores_whitespace_but_tracks_changes() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Microsoft Word",
            "季度复盘方案.docx",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 4, 1);
        let mut first = make_candidate(&service, timeline_id);
        first.capture_ax_text = Some("第一章  背景\n第二章  方案".to_string());
        let mut same = first.clone();
        same.capture_ax_text = Some("第一章 背景 第二章 方案".to_string());
        let mut changed = first.clone();
        changed.capture_ax_text = Some("第一章 背景 第二章 新方案".to_string());

        assert_eq!(
            artifact_source_fingerprint(&first),
            artifact_source_fingerprint(&same)
        );
        assert_ne!(
            artifact_source_fingerprint(&first),
            artifact_source_fingerprint(&changed)
        );
    }

    #[test]
    fn test_generated_document_title_removes_incremental_wording() {
        assert_eq!(
            sanitize_generated_document_title("文档增量：3BU AI 能力盘点"),
            "3BU AI 能力盘点"
        );
        assert_eq!(
            sanitize_generated_document_title("商业体系 AI 建设方案"),
            "商业体系 AI 建设方案"
        );
    }

    #[test]
    fn test_document_url_variants_reserve_only_one_task() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "弹性伸缩 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 2, 1);
        let mut first = make_candidate(&service, timeline_id);
        first.capture_ax_text = Some("文档正文".repeat(80));
        first.capture_url =
            Some("https://docs.example.com/k/home/space/document-id?from=home".to_string());
        let mut second = first.clone();
        second.capture_url =
            Some("https://docs.example.com/k/home/space/document-id#section".to_string());
        let mut queued = std::collections::HashSet::new();

        assert!(reserve_document_task(&first, &mut queued));
        assert!(!reserve_document_task(&second, &mut queued));
        assert_eq!(
            queued,
            std::collections::HashSet::from([String::from(
                "https://docs.example.com/k/home/space/document-id"
            )])
        );
    }

    use crate::storage::models::{EventType, NewCapture};

    fn make_service() -> BakeService {
        let storage = StorageManager::open_in_memory().expect("内存数据库初始化失败");
        BakeService::new(storage, "http://127.0.0.1:7071")
    }

    fn test_document_request(
        title: &str,
        doc_type: &str,
        source_url: Option<&str>,
    ) -> CreateOrUpdateDocumentRequest {
        CreateOrUpdateDocumentRequest {
            title: title.to_string(),
            doc_type: doc_type.to_string(),
            status: "enabled".to_string(),
            tags: Vec::new(),
            applicable_tasks: Vec::new(),
            source_memory_ids: Vec::new(),
            source_capture_ids: Vec::new(),
            source_episode_ids: Vec::new(),
            linked_knowledge_ids: Vec::new(),
            sections: Vec::new(),
            style_phrases: Vec::new(),
            replacement_rules: Vec::new(),
            summary: Some(format!("{title}摘要")),
            full_content: Some(format!("{title}内容")),
            structured_content: None,
            prompt_hint: None,
            diagram_code: None,
            image_assets: Vec::new(),
            source_app_name: None,
            source_win_title: None,
            source_url: source_url.map(ToString::to_string),
            content_hash: None,
            language: None,
            usage_count: None,
            match_score: None,
            match_level: None,
            creation_mode: Some("manual".to_string()),
            review_status: Some("confirmed".to_string()),
            evidence_summary: None,
            generation_version: None,
            deleted_at: None,
        }
    }

    #[test]
    fn test_document_list_filters_multi_type_and_source_url() {
        let service = make_service();
        service
            .create_document(test_document_request(
                "项目复盘",
                "weekly_report,project_plan",
                Some("https://docs.example.com/projects/alpha"),
            ))
            .unwrap();
        service
            .create_document(test_document_request(
                "普通说明",
                "general_document",
                Some("https://docs.example.com/guides/start"),
            ))
            .unwrap();

        let by_type = service
            .list_documents_paginated_with_type(
                BakeListFilter {
                    limit: 20,
                    ..BakeListFilter::default()
                },
                Some("project_plan"),
            )
            .unwrap();
        assert_eq!(by_type.total, 1);
        assert_eq!(by_type.items[0].title, "项目复盘");

        let by_url = service
            .list_documents_paginated(BakeListFilter {
                q: Some("projects/alpha".to_string()),
                limit: 20,
                ..BakeListFilter::default()
            })
            .unwrap();
        assert_eq!(by_url.total, 1);
        assert_eq!(
            by_url.items[0].source_url.as_deref(),
            Some("https://docs.example.com/projects/alpha")
        );
    }

    fn seed_capture(service: &BakeService, ts: i64, app_name: &str, title: &str) -> i64 {
        service
            .storage
            .insert_capture(&NewCapture {
                ts,
                app_name: Some(app_name.to_string()),
                app_bundle_id: Some(format!("com.example.{app_name}")),
                win_title: Some(title.to_string()),
                event_type: EventType::Manual,
                ax_text: Some("原文内容".to_string()),
                ax_focused_role: None,
                ax_focused_id: None,
                ocr_text: None,
                screenshot_path: None,
                screenshot_source: None,
                input_text: None,
                url: None,
                webpage_title: None,
                is_sensitive: false,
                pii_scrubbed: false,
            })
            .expect("插入 capture 失败")
    }

    #[test]
    fn test_list_capture_records_filters_by_app_name() {
        let service = make_service();
        seed_capture(&service, 1_710_000_000_000, "Safari", "项目文档");
        seed_capture(&service, 1_710_000_001_000, "Code", "代码编辑器");

        let response = service
            .list_capture_records_paginated(BakeCaptureFilter {
                app_name: Some("Safari".to_string()),
                limit: 20,
                ..BakeCaptureFilter::default()
            })
            .expect("按应用筛选采集记录失败");

        assert_eq!(response.total, 1);
        assert_eq!(response.items.len(), 1);
        assert_eq!(response.items[0].app_name.as_deref(), Some("Safari"));
    }

    fn seed_knowledge(
        service: &BakeService,
        category: &str,
        capture_id: i64,
        importance: i64,
        occurrence_count: i64,
    ) -> i64 {
        service
            .storage
            .insert_timeline_entry(&NewTimeline {
                capture_id,
                summary: format!("{category}-summary-{capture_id}"),
                overview: Some("知识摘要".to_string()),
                details: Some("{}".to_string()),
                entities: r#"["模板","流程"]"#.to_string(),
                category: category.to_string(),
                importance,
                occurrence_count: Some(occurrence_count),
                observed_at: Some(1_710_000_000_000),
                event_time_start: None,
                event_time_end: None,
                history_view: true,
                content_origin: Some("historical_content".to_string()),
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
            .expect("插入 knowledge 失败")
    }

    #[test]
    fn test_knowledge_list_searches_and_returns_source_url() {
        let service = make_service();
        // 先占用一个 capture ID，确保下面的真实 capture ID 与 timeline ID 不同，
        // 从而能检测 timeline/capture 命名空间是否再次混用。
        seed_capture(&service, 1_709_999_999_000, "Code", "占位采集");
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Safari",
            "MemoryBread API 文档",
        );
        service
            .storage
            .with_conn(|conn| {
                conn.execute(
                    "UPDATE captures SET url = ?1 WHERE id = ?2",
                    rusqlite::params!["https://kb.example.com/memorybread/api", capture_id],
                )
                .map_err(StorageError::Sqlite)?;
                Ok(())
            })
            .unwrap();
        let timeline_id = seed_knowledge(&service, "knowledge", capture_id, 7, 1);
        service
            .storage
            .insert_bake_knowledge(&NewBakeKnowledge {
                timeline_id,
                title: "接口约定".to_string(),
                summary: "本地接口约定".to_string(),
                content: Some(
                    json!({"status": "confirmed", "review_status": "confirmed"}).to_string(),
                ),
                detailed_content: Some("说明本地接口的调用方式".to_string()),
                entities: "[]".to_string(),
                importance: 7,
                source_capture_ids: Some(format!("[{capture_id}]")),
            })
            .unwrap();

        let response = service
            .list_knowledge_paginated(BakeListFilter {
                q: Some("kb.example.com/memorybread".to_string()),
                limit: 20,
                ..BakeListFilter::default()
            })
            .unwrap();

        assert_eq!(response.total, 1);
        assert_eq!(response.items[0].capture_id, capture_id.to_string());
        assert_eq!(
            response.items[0].source_capture_ids,
            vec![capture_id.to_string()]
        );
        assert_eq!(
            response.items[0].source_timeline_id,
            timeline_id.to_string()
        );
        assert_ne!(
            response.items[0].capture_id,
            response.items[0].source_timeline_id
        );
        assert_eq!(
            response.items[0].source_url.as_deref(),
            Some("https://kb.example.com/memorybread/api")
        );
    }

    fn link_captures_to_timeline(service: &BakeService, timeline_id: i64, capture_ids: &[i64]) {
        service
            .storage
            .with_conn(|conn| {
                for capture_id in capture_ids {
                    conn.execute(
                        "UPDATE captures SET timeline_id = ?1 WHERE id = ?2",
                        rusqlite::params![timeline_id, capture_id],
                    )
                    .map_err(StorageError::Sqlite)?;
                }
                Ok(())
            })
            .expect("关联 capture 到 timeline 失败");
    }

    fn make_candidate(service: &BakeService, timeline_id: i64) -> BakeMemorySourceRecord {
        let timeline = service
            .storage
            .get_timeline_entry(timeline_id)
            .expect("查询 timeline 失败")
            .expect("timeline 不存在");
        BakeMemorySourceRecord {
            timeline,
            capture_ts: 1_710_000_000_000,
            capture_app_name: Some("Code".to_string()),
            capture_win_title: Some("知识来源".to_string()),
            capture_ax_text: Some("候选文本".to_string()),
            capture_ocr_text: None,
            capture_input_text: None,
            capture_audio_text: None,
            capture_url: None,
            capture_webpage_title: None,
            preferred_source_title: None,
            url_aggregated_text: None,
            url_aggregated_capture_count: 0,
            action_trace: Vec::new(),
            work_item: None,
            work_status: None,
            work_progress: None,
            retry_failure_count: 0,
            retry_error_code: None,
            retry_next_at_ms: 0,
        }
    }

    fn operation_trace(capture_ids: &[i64]) -> Vec<BakeActionTraceRecord> {
        capture_ids
            .iter()
            .enumerate()
            .map(|(index, capture_id)| BakeActionTraceRecord {
                capture_id: *capture_id,
                ts: 1_710_000_000_000 + index as i64 * 1_000,
                event_type: "manual".to_string(),
                app_name: Some("Code".to_string()),
                win_title: Some(format!("操作步骤 {}", index + 1)),
                url: None,
                webpage_title: None,
                visible_text: Some(format!("操作证据 {}", index + 1)),
                input_text: None,
                audio_text: None,
                ax_focused_role: Some("AXButton".to_string()),
                ax_focused_id: Some(format!("step-{}", index + 1)),
                state_delta: (index > 0).then(|| format!("visible→操作证据 {}", index + 1)),
                evidence_kind: Some(if index == 0 { "input" } else { "state_change" }.to_string()),
                evidence_role: Some(if index == 0 { "action" } else { "result" }.to_string()),
                evidence_reason: Some(
                    if index == 0 {
                        "explicit_input"
                    } else {
                        "observable_state_result"
                    }
                    .to_string(),
                ),
                operation_evidence: true,
            })
            .collect()
    }

    #[test]
    fn test_extract_candidate_contract_reports_source_capture_count() {
        let service = make_service();
        let primary = seed_capture(&service, 1_710_000_000_000, "Code", "开始排查");
        let timeline_id = seed_knowledge(&service, "coding", primary, 4, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.timeline.capture_ids = Some(format!("[{},{}]", primary, primary + 1));
        candidate.timeline.start_time = Some(1_710_000_000_000);
        candidate.timeline.end_time = Some(1_710_000_010_000);
        candidate.timeline.key_timestamps = Some(format!(
            r#"[{{"capture_id":{},"ts":1710000000000}}]"#,
            primary
        ));
        candidate.action_trace = vec![
            BakeActionTraceRecord {
                capture_id: primary,
                ts: 1_710_000_000_000,
                event_type: "manual".to_string(),
                app_name: Some("Code".to_string()),
                win_title: Some("开始排查".to_string()),
                url: None,
                webpage_title: None,
                visible_text: Some("检查配置".to_string()),
                input_text: None,
                audio_text: None,
                ax_focused_role: Some("AXTextArea".to_string()),
                ax_focused_id: Some("editor".to_string()),
                state_delta: None,
                evidence_kind: Some("interaction".to_string()),
                evidence_role: Some("action".to_string()),
                evidence_reason: Some("focused_control_interaction".to_string()),
                operation_evidence: true,
            },
            BakeActionTraceRecord {
                capture_id: primary + 1,
                ts: 1_710_000_010_000,
                event_type: "manual".to_string(),
                app_name: Some("Terminal".to_string()),
                win_title: Some("测试".to_string()),
                url: None,
                webpage_title: None,
                visible_text: Some("验证通过".to_string()),
                input_text: Some("cargo test".to_string()),
                audio_text: None,
                ax_focused_role: Some("AXTextField".to_string()),
                ax_focused_id: Some("terminal".to_string()),
                state_delta: Some("window:Code→Terminal; visible→验证通过".to_string()),
                evidence_kind: Some("state_change".to_string()),
                evidence_role: Some("result".to_string()),
                evidence_reason: Some("observable_state_result".to_string()),
                operation_evidence: true,
            },
        ];

        let payload = map_extract_candidate_payload(&candidate);

        assert_eq!(payload.source_capture_count, 2);
        assert_eq!(payload.effective_capture_count, 2);
        assert_eq!(payload.action_trace.len(), 2);
        assert_eq!(
            payload.action_trace[1].app_name.as_deref(),
            Some("Terminal")
        );
        assert_eq!(payload.start_time, Some(1_710_000_000_000));
        assert!(payload.key_timestamps.is_some());
    }

    #[test]
    fn test_sop_audit_accepts_two_direct_nodes_when_action_has_observable_result() {
        let service = make_service();
        let first = seed_capture(&service, 1_710_000_000_000, "Code", "步骤一");
        let second = seed_capture(&service, 1_710_000_001_000, "Code", "步骤二");
        let third = seed_capture(&service, 1_710_000_002_000, "Code", "滚动上下文");
        let timeline_id = seed_knowledge(&service, "coding", first, 4, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.timeline.capture_ids = Some(format!("[{first},{second},{third}]"));
        candidate.action_trace = operation_trace(&[first, second, third]);
        candidate.action_trace[2].operation_evidence = false;
        candidate.action_trace[2].evidence_kind = Some("context".to_string());
        candidate.action_trace[2].evidence_role = Some("context".to_string());
        candidate.action_trace[2].evidence_reason = Some("passive_context".to_string());

        let audit = new_bake_candidate_audit(1, &candidate, "queued", None);

        assert_eq!(audit.effective_capture_count, 2);
        assert!(audit.sop_eligible);
        assert_eq!(
            audit.sop_eligibility_reason.as_deref(),
            Some("eligible_direct_action_result")
        );
        assert_eq!(
            audit.sop_evidence_mode.as_deref(),
            Some("direct_interaction")
        );
    }

    #[test]
    fn test_sop_eligibility_rejects_navigation_only_trace() {
        let service = make_service();
        let first = seed_capture(&service, 1_710_000_000_000, "Chrome", "文档一");
        let second = seed_capture(&service, 1_710_000_001_000, "Chrome", "文档二");
        let timeline_id = seed_knowledge(&service, "文档", first, 4, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.timeline.capture_ids = Some(format!("[{first},{second}]"));
        candidate.action_trace = operation_trace(&[first, second]);
        for record in &mut candidate.action_trace {
            record.event_type = "browser_navigation".to_string();
            record.evidence_kind = Some("navigation".to_string());
            record.evidence_role = Some("context".to_string());
            record.evidence_reason = Some("navigation_only".to_string());
            record.operation_evidence = false;
        }

        let eligibility = sop_eligibility(&candidate);

        assert!(!eligibility.eligible);
        assert_eq!(eligibility.reason, "passive_report_only");
        assert!(eligibility.mode.is_none());
    }

    #[test]
    fn test_sop_eligibility_rejects_passive_agent_report_like_id_930() {
        let service = make_service();
        let first = seed_capture(&service, 1_710_000_000_000, "MyFlicker", "WorkBuddy");
        let second = seed_capture(&service, 1_710_000_001_000, "MyFlicker", "WorkBuddy");
        let third = seed_capture(&service, 1_710_000_002_000, "MyFlicker", "WorkBuddy");
        let timeline_id = seed_knowledge(&service, "coding", first, 4, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.timeline.capture_ids = Some(format!("[{first},{second},{third}]"));
        candidate.timeline.activity_type = Some("coding".to_string());
        candidate.timeline.summary = "Agent 汇报已修改代码并且测试通过".to_string();
        candidate.work_status = Some("completed".to_string());
        candidate.work_progress = Some("执行完成，Lint 检查通过".to_string());
        candidate.action_trace = operation_trace(&[first, second, third]);
        for (record, event_type) in
            candidate
                .action_trace
                .iter_mut()
                .zip(["app_switch", "key_pause", "auto"])
        {
            record.event_type = event_type.to_string();
            record.input_text = None;
            record.evidence_kind = Some("context".to_string());
            record.evidence_role = Some("context".to_string());
            record.evidence_reason = Some("agent_report_surface".to_string());
            record.operation_evidence = false;
        }

        let eligibility = sop_eligibility(&candidate);

        assert!(!eligibility.eligible);
        assert_eq!(eligibility.reason, "passive_report_only");
        assert!(eligibility.mode.is_none());
        assert_eq!(eligibility.effective_capture_count, 0);
    }

    #[test]
    fn test_sop_eligibility_rejects_semantic_workflow_without_action_anchor() {
        let service = make_service();
        let first = seed_capture(&service, 1_710_000_000_000, "Code", "修复实现");
        let second = seed_capture(&service, 1_710_000_001_000, "Terminal", "测试通过");
        let timeline_id = seed_knowledge(&service, "代码", first, 4, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.timeline.capture_ids = Some(format!("[{first},{second}]"));
        candidate.timeline.activity_type = Some("coding".to_string());
        candidate.timeline.summary = "修复时间线提炼并完成验证".to_string();
        candidate.timeline.details = Some("修改证据门禁，运行测试后全部通过".to_string());
        candidate.work_item = Some("MemoryBread-操作提炼".to_string());
        candidate.work_status = Some("completed".to_string());
        candidate.work_progress = Some("已完成实现，测试通过".to_string());
        candidate.action_trace = operation_trace(&[first, second]);
        for record in &mut candidate.action_trace {
            record.operation_evidence = false;
            record.evidence_kind = Some("context".to_string());
            record.evidence_role = Some("context".to_string());
            record.evidence_reason = Some("passive_context".to_string());
        }

        let eligibility = sop_eligibility(&candidate);

        assert!(!eligibility.eligible);
        assert_eq!(eligibility.reason, "missing_real_action");
        assert_eq!(eligibility.mode, None);
        assert_eq!(eligibility.effective_capture_count, 0);
    }

    #[test]
    fn test_merge_bake_candidate_lanes_reserves_retry_quota_without_starving_fresh() {
        let service = make_service();
        let capture_id = seed_capture(&service, 1_710_000_000_000, "Code", "候选");
        let timeline_id = seed_knowledge(&service, "coding", capture_id, 4, 1);
        let template = make_candidate(&service, timeline_id);
        let fresh = (0..60)
            .map(|offset| {
                let mut candidate = template.clone();
                candidate.timeline.id = 1_000 + offset;
                candidate
            })
            .collect::<Vec<_>>();
        let retry = (0..60)
            .map(|offset| {
                let mut candidate = template.clone();
                candidate.timeline.id = 2_000 + offset;
                candidate.retry_failure_count = 1;
                candidate.retry_error_code = Some("BAKE_OUTPUT_INVALID".to_string());
                candidate
            })
            .collect::<Vec<_>>();

        let merged = merge_bake_candidate_lanes(fresh, retry, 10, 0);
        let retry_count = merged
            .iter()
            .filter(|candidate| candidate.retry_failure_count > 0)
            .count();
        assert_eq!(merged.len(), 10);
        assert_eq!(retry_count, 2);
        assert_eq!(merged[0].retry_failure_count, 1);
        assert_eq!(merged[5].retry_failure_count, 1);
    }

    #[test]
    fn test_persist_sop_rejects_single_capture_even_when_sidecar_accepts() {
        let service = make_service();
        let capture_id = seed_capture(&service, 1_710_000_000_000, "Code", "单帧设置页");
        let timeline_id = seed_knowledge(&service, "coding", capture_id, 4, 1);
        let candidate = make_candidate(&service, timeline_id);
        let extraction = BakeArtifactExtraction {
            accepted: true,
            reason: None,
            payload: Some(json!({
                "summary": "模型推测的单帧 SOP",
                "steps": ["打开设置", "修改选项", "保存"]
            })),
        };
        let mut existing_sources = std::collections::HashSet::new();

        let result = service
            .persist_sop_artifact(None, &candidate, "test", &extraction, &mut existing_sources)
            .expect("单帧守卫不应返回错误");

        assert_eq!(result.discarded_count, 1);
        assert_eq!(result.sop_created_count, 0);
        assert_eq!(service.storage.count_bake_sops().unwrap(), 0);
    }

    #[test]
    fn test_persist_sop_rejects_missing_step_evidence_without_timeline_fallback() {
        let service = make_service();
        let first = seed_capture(&service, 1_710_000_000_000, "Code", "检查配置");
        let second = seed_capture(&service, 1_710_000_001_000, "Code", "执行修复");
        let third = seed_capture(&service, 1_710_000_002_000, "Code", "验证结果");
        let timeline_id = seed_knowledge(&service, "coding", first, 4, 1);
        link_captures_to_timeline(&service, timeline_id, &[first, second, third]);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.timeline.capture_ids = Some(format!("[{first},{second},{third}]"));
        candidate.action_trace = operation_trace(&[first, second, third]);
        let extraction = BakeArtifactExtraction {
            accepted: true,
            reason: None,
            payload: Some(json!({
                "summary": "缺少逐步证据的 SOP",
                "steps": ["检查配置", "执行修复", "验证结果"],
                "step_evidence": [
                    {"step_index": 1, "capture_ids": [first.to_string()]},
                    {"step_index": 3, "capture_ids": [third.to_string()]}
                ]
            })),
        };
        let mut existing_sources = std::collections::HashSet::new();

        let result = service
            .persist_sop_artifact(None, &candidate, "test", &extraction, &mut existing_sources)
            .expect("缺少逐步证据应作为可解释拒绝处理");

        assert_eq!(result.sop_created_count, 0);
        assert_eq!(result.sop_persist_status, Some("rejected"));
        assert_eq!(
            result.sop_persist_reason.as_deref(),
            Some("missing_sop_step_evidence")
        );
        assert_eq!(service.storage.count_bake_sops().unwrap(), 0);
    }

    #[tokio::test]
    async fn test_bundle_persistence_keeps_valid_sop_when_knowledge_payload_is_invalid() {
        let service = make_service();
        let first = seed_capture(&service, 1_710_000_000_000, "Code", "检查配置");
        let second = seed_capture(&service, 1_710_000_001_000, "Code", "执行修复");
        let third = seed_capture(&service, 1_710_000_002_000, "Terminal", "验证通过");
        let timeline_id = seed_knowledge(&service, "代码", first, 4, 1);
        link_captures_to_timeline(&service, timeline_id, &[first, second, third]);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.timeline.capture_ids = Some(format!("[{first},{second},{third}]"));
        candidate.action_trace = operation_trace(&[first, second, third]);
        let extracted = BakeExtractResponse {
            knowledge: BakeArtifactExtraction {
                accepted: true,
                reason: None,
                payload: Some(json!(["malformed-knowledge-payload"])),
            },
            document: BakeArtifactExtraction {
                accepted: false,
                reason: Some("not_a_document".to_string()),
                payload: None,
            },
            sop: BakeArtifactExtraction {
                accepted: true,
                reason: None,
                payload: Some(json!({
                    "summary": "检查配置、修复并验证",
                    "steps": ["检查配置", "执行修复", "运行测试验证结果"],
                    "step_evidence": [
                        {"step_index": 1, "capture_ids": [first.to_string()]},
                        {"step_index": 2, "capture_ids": [second.to_string()]},
                        {"step_index": 3, "capture_ids": [third.to_string()]}
                    ]
                })),
            },
            primary_type: Some("sop".to_string()),
            classification_reason: None,
            usage: None,
            model: None,
            degraded: Some(true),
            artifact_shapes: None,
            compatibility_recovered: None,
        };
        let mut knowledge_sources = std::collections::HashSet::new();
        let mut document_sources = std::collections::HashSet::new();
        let mut document_urls = std::collections::HashSet::new();
        let mut sop_sources = std::collections::HashSet::new();

        let result = service
            .persist_extracted_candidate(
                None,
                None,
                &candidate,
                "test",
                extracted,
                &mut knowledge_sources,
                &mut document_sources,
                &mut document_urls,
                &mut sop_sources,
            )
            .await
            .expect("知识 payload 失败不应阻止有效 SOP 持久化");

        assert_eq!(result.sop_created_count, 1);
        assert_eq!(service.storage.count_bake_sops().unwrap(), 1);
        assert_eq!(service.storage.count_bake_knowledge().unwrap(), 0);
    }

    #[test]
    fn test_artifact_audit_maps_sidecar_design_contract_to_document_branch() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "招聘方案 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 3, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url = Some("https://docs.example.com/d/home/recruiting".to_string());
        candidate.capture_ax_text = Some("招聘方案正文".repeat(100));
        let run_id = service
            .storage
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "test".to_string(),
                status: "running".to_string(),
                started_at: 1_710_000_000_000,
            })
            .unwrap();
        let extracted = BakeExtractResponse {
            knowledge: BakeArtifactExtraction {
                accepted: false,
                reason: Some("invalid_json".to_string()),
                payload: None,
            },
            document: BakeArtifactExtraction {
                accepted: true,
                reason: None,
                payload: Some(json!({
                    "name": "招聘方案",
                    "full_content": "招聘方案正文",
                    "summary": "招聘方案摘要"
                })),
            },
            sop: BakeArtifactExtraction {
                accepted: false,
                reason: Some("missing_real_action".to_string()),
                payload: None,
            },
            primary_type: Some("knowledge".to_string()),
            classification_reason: None,
            usage: None,
            model: None,
            degraded: Some(true),
            artifact_shapes: Some(json!({
                "knowledge": "null",
                "design": "array_single_recovered",
                "sop": "object"
            })),
            compatibility_recovered: Some(json!({
                "knowledge": false,
                "design": true,
                "sop": false
            })),
        };

        service
            .record_artifact_extraction_audits(run_id, &candidate, &extracted)
            .unwrap();
        let audits = service
            .storage
            .list_bake_artifact_audits_for_timeline(timeline_id, 10)
            .unwrap();
        let document = audits
            .iter()
            .find(|audit| audit.artifact_kind == "document")
            .unwrap();
        assert_eq!(
            document.artifact_shape.as_deref(),
            Some("array_single_recovered")
        );
        assert!(document.compatibility_recovered);
        assert_eq!(document.deterministic_eligible, Some(true));
    }

    #[tokio::test]
    async fn test_strong_document_evidence_model_rejection_is_retryable_false_negative() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "招聘方案 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 3, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url = Some("https://docs.example.com/d/home/recruiting".to_string());
        candidate.capture_webpage_title = Some("招聘方案 - 云文档".to_string());
        candidate.capture_ax_text = Some("岗位职责与任职要求".repeat(100));
        let extracted = BakeExtractResponse {
            knowledge: BakeArtifactExtraction {
                accepted: false,
                reason: Some("not_knowledge".to_string()),
                payload: None,
            },
            document: BakeArtifactExtraction {
                accepted: false,
                reason: Some("not_a_document".to_string()),
                payload: None,
            },
            sop: BakeArtifactExtraction {
                accepted: false,
                reason: Some("missing_real_action".to_string()),
                payload: None,
            },
            primary_type: Some("knowledge".to_string()),
            classification_reason: None,
            usage: None,
            model: None,
            degraded: Some(false),
            artifact_shapes: None,
            compatibility_recovered: None,
        };
        let mut knowledge_sources = std::collections::HashSet::new();
        let mut document_sources = std::collections::HashSet::new();
        let mut document_urls = std::collections::HashSet::new();
        let mut sop_sources = std::collections::HashSet::new();

        let error = service
            .persist_extracted_candidate(
                Some(9_999),
                None,
                &candidate,
                "test",
                extracted,
                &mut knowledge_sources,
                &mut document_sources,
                &mut document_urls,
                &mut sop_sources,
            )
            .await
            .expect_err("强文档证据不应被模型拒绝后永久跳过");

        assert_eq!(
            bake_retry_error_code(&error),
            "BAKE_DOCUMENT_FALSE_NEGATIVE"
        );
        assert!(is_retryable_bake_candidate_error(&error));
    }

    #[tokio::test]
    async fn test_final_document_retry_recovers_review_candidate_from_captured_source() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "招聘方案 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 3, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url = Some("https://docs.example.com/d/home/recruiting".to_string());
        candidate.capture_webpage_title = Some("招聘方案 - 云文档".to_string());
        let captured_body = "岗位职责与任职要求".repeat(100);
        candidate.capture_ax_text = Some(captured_body.clone());
        candidate.retry_failure_count = MAX_BAKE_RETRY_FAILURES - 1;
        let extracted = BakeExtractResponse {
            knowledge: BakeArtifactExtraction {
                accepted: false,
                reason: Some("not_knowledge".to_string()),
                payload: None,
            },
            document: BakeArtifactExtraction {
                accepted: false,
                reason: Some("not_a_document".to_string()),
                payload: None,
            },
            sop: BakeArtifactExtraction {
                accepted: false,
                reason: Some("missing_real_action".to_string()),
                payload: None,
            },
            primary_type: Some("knowledge".to_string()),
            classification_reason: None,
            usage: None,
            model: None,
            degraded: Some(false),
            artifact_shapes: Some(json!({"design": "object"})),
            compatibility_recovered: Some(json!({"design": false})),
        };
        let run_id = service
            .storage
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "test".to_string(),
                status: "running".to_string(),
                started_at: 1_710_000_000_000,
            })
            .unwrap();
        service
            .record_artifact_extraction_audits(run_id, &candidate, &extracted)
            .unwrap();
        let mut knowledge_sources = std::collections::HashSet::new();
        let mut document_sources = std::collections::HashSet::new();
        let mut document_urls = std::collections::HashSet::new();
        let mut sop_sources = std::collections::HashSet::new();

        let result = service
            .persist_extracted_candidate(
                Some(run_id),
                None,
                &candidate,
                "test",
                extracted,
                &mut knowledge_sources,
                &mut document_sources,
                &mut document_urls,
                &mut sop_sources,
            )
            .await
            .expect("最后一次强文档证据应从采集正文恢复待审核文档");

        assert_eq!(result.document_created_count, 1);
        let document = service
            .storage
            .find_bake_document_by_source_memory_id(timeline_id)
            .unwrap()
            .expect("应创建来源文档");
        assert_eq!(document.review_status, "candidate");
        assert_eq!(
            document.full_content.as_deref(),
            Some(captured_body.as_str())
        );
        assert_eq!(document.summary, None);
        let audits = service
            .storage
            .list_bake_artifact_audits_for_timeline(timeline_id, 10)
            .unwrap();
        let document_audit = audits
            .iter()
            .find(|audit| audit.artifact_kind == "document")
            .unwrap();
        assert_eq!(document_audit.model_accepted, Some(false));
        assert_eq!(document_audit.persist_status, "recovered_from_source");
        assert_eq!(document_audit.artifact_id, Some(document.id));
    }

    #[tokio::test]
    async fn test_final_document_retry_recovers_when_accepted_payload_is_invalid() {
        let service = make_service();
        let capture_id = seed_capture(
            &service,
            1_710_000_000_000,
            "Google Chrome",
            "招聘方案 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "文档", capture_id, 3, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url = Some("https://docs.example.com/d/home/invalid-payload".to_string());
        candidate.capture_webpage_title = Some("招聘方案 - 云文档".to_string());
        candidate.capture_ax_text = Some("岗位职责与任职要求".repeat(100));
        candidate.retry_failure_count = MAX_BAKE_RETRY_FAILURES - 1;
        let extracted = BakeExtractResponse {
            knowledge: BakeArtifactExtraction {
                accepted: false,
                reason: Some("not_knowledge".to_string()),
                payload: None,
            },
            document: BakeArtifactExtraction {
                accepted: true,
                reason: None,
                payload: Some(json!(["malformed-document-payload"])),
            },
            sop: BakeArtifactExtraction {
                accepted: false,
                reason: Some("missing_real_action".to_string()),
                payload: None,
            },
            primary_type: Some("document".to_string()),
            classification_reason: None,
            usage: None,
            model: None,
            degraded: Some(true),
            artifact_shapes: Some(json!({"design": "array_invalid"})),
            compatibility_recovered: Some(json!({"design": false})),
        };
        let mut knowledge_sources = std::collections::HashSet::new();
        let mut document_sources = std::collections::HashSet::new();
        let mut document_urls = std::collections::HashSet::new();
        let mut sop_sources = std::collections::HashSet::new();

        let result = service
            .persist_extracted_candidate(
                Some(9_999),
                None,
                &candidate,
                "test",
                extracted,
                &mut knowledge_sources,
                &mut document_sources,
                &mut document_urls,
                &mut sop_sources,
            )
            .await
            .expect("最终尝试的无效文档 payload 应从采集正文恢复");

        assert_eq!(result.document_created_count, 1);
        let document = service
            .storage
            .find_bake_document_by_source_memory_id(timeline_id)
            .unwrap()
            .unwrap();
        assert_eq!(document.review_status, "candidate");
    }

    #[tokio::test]
    async fn test_non_document_model_rejection_does_not_trigger_document_retry() {
        let service = make_service();
        let capture_id = seed_capture(&service, 1_710_000_000_000, "Kim", "项目群");
        let timeline_id = seed_knowledge(&service, "会议", capture_id, 3, 1);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_app_name = Some("Kim".to_string());
        candidate.capture_win_title = Some("项目群".to_string());
        candidate.capture_url = None;
        candidate.capture_ax_text = Some("群聊中提到招聘方案文档".repeat(30));
        let extracted = BakeExtractResponse {
            knowledge: BakeArtifactExtraction {
                accepted: false,
                reason: Some("not_knowledge".to_string()),
                payload: None,
            },
            document: BakeArtifactExtraction {
                accepted: false,
                reason: Some("not_a_document".to_string()),
                payload: None,
            },
            sop: BakeArtifactExtraction {
                accepted: false,
                reason: Some("missing_real_action".to_string()),
                payload: None,
            },
            primary_type: Some("knowledge".to_string()),
            classification_reason: None,
            usage: None,
            model: None,
            degraded: Some(false),
            artifact_shapes: None,
            compatibility_recovered: None,
        };
        let mut knowledge_sources = std::collections::HashSet::new();
        let mut document_sources = std::collections::HashSet::new();
        let mut document_urls = std::collections::HashSet::new();
        let mut sop_sources = std::collections::HashSet::new();

        let result = service
            .persist_extracted_candidate(
                Some(9_999),
                None,
                &candidate,
                "test",
                extracted,
                &mut knowledge_sources,
                &mut document_sources,
                &mut document_urls,
                &mut sop_sources,
            )
            .await
            .expect("非文档候选的模型拒绝应正常完成");

        assert_eq!(result.document_created_count, 0);
    }

    #[test]
    fn test_initialize_memories_is_idempotent() {
        let service = make_service();
        let capture_id = seed_capture(&service, 1_710_000_000_000, "Chrome", "方案页");
        seed_knowledge(&service, "meeting", capture_id, 4, 3);

        let first = service.initialize_memories(10).expect("首次初始化失败");
        assert_eq!(first.created_count, 0);
        assert_eq!(first.skipped_count, 1);
        assert!(first.articles.is_empty());

        let second = service.initialize_memories(10).expect("二次初始化失败");
        assert_eq!(second.created_count, 0);
        assert_eq!(second.skipped_count, 1);
    }

    #[test]
    fn test_list_knowledge_by_heat_uses_source_timeline_occurrences() {
        let service = make_service();
        let cold_capture = seed_capture(&service, 1_710_000_000_000, "Code", "低热度来源");
        let hot_capture = seed_capture(&service, 1_710_000_010_000, "Code", "高热度来源");
        let cold_timeline = seed_knowledge(&service, "meeting", cold_capture, 5, 1);
        let hot_timeline = seed_knowledge(&service, "meeting", hot_capture, 3, 12);

        for (timeline_id, title) in [(cold_timeline, "低热度知识"), (hot_timeline, "高热度知识")]
        {
            service
                .storage
                .insert_bake_knowledge(&NewBakeKnowledge {
                    timeline_id,
                    title: title.to_string(),
                    summary: title.to_string(),
                    content: Some(r#"{"status":"confirmed"}"#.to_string()),
                    detailed_content: None,
                    entities: "[]".to_string(),
                    importance: 3,
                    source_capture_ids: None,
                })
                .expect("插入 bake knowledge 失败");
        }

        let response = service
            .list_knowledge_paginated(BakeListFilter {
                q: None,
                bucket: None,
                from_ts: None,
                to_ts: None,
                favorite: None,
                limit: 1,
                offset: 0,
                sort: BakeListSort::Heat,
            })
            .expect("按热度查询 knowledge 失败");

        assert_eq!(response.items.len(), 1);
        assert_eq!(response.items[0].summary, "高热度知识");
        assert_eq!(response.items[0].occurrence_count, 12);

        let searched = service
            .list_knowledge_paginated(BakeListFilter {
                q: Some("低热度".to_string()),
                bucket: None,
                from_ts: None,
                to_ts: None,
                favorite: None,
                limit: 10,
                offset: 0,
                sort: BakeListSort::Recent,
            })
            .expect("搜索 knowledge 失败");
        assert_eq!(searched.items.len(), 1);
        assert_eq!(searched.items[0].summary, "低热度知识");
    }

    #[test]
    fn test_infer_suggested_action() {
        assert_eq!(
            infer_suggested_action(&["SOP".to_string()]),
            Some("sop".to_string())
        );
        assert_eq!(
            infer_suggested_action(&["技术方案".to_string()]),
            Some("design".to_string())
        );
    }

    #[test]
    fn test_resolve_review_status_always_auto_created() {
        assert_eq!(
            resolve_review_status(Some("candidate"), Some(0.91), Some("high")),
            "auto_created"
        );
        assert_eq!(
            resolve_review_status(Some("candidate"), Some(0.91), Some("medium")),
            "auto_created"
        );
        assert_eq!(
            resolve_review_status(Some("candidate"), Some(0.60), Some("high")),
            "auto_created"
        );
        assert_eq!(
            resolve_review_status(Some("candidate"), Some(0.72), Some("high")),
            "auto_created"
        );
        assert_eq!(
            resolve_review_status(Some("auto_created"), Some(0.95), Some("low")),
            "auto_created"
        );
    }

    #[test]
    fn test_knowledge_decision_uses_final_threshold_and_open_semantics() {
        let payload = |score: f64, include_semantics: bool| {
            serde_json::from_value::<BakeKnowledgeArtifactPayload>(json!({
                "summary": "可复用事实",
                "evidence_summary": "来源明确记录了对象、状态和观测时间",
                "future_question": include_semantics.then_some("后续执行应依据什么事实？"),
                "decision_reason": include_semantics.then_some("该事实会改变后续执行和验证方式"),
                "match_score": score
            }))
            .unwrap()
        };

        assert_eq!(
            resolve_knowledge_decision(&payload(0.78, true)).state,
            "published"
        );
        assert_eq!(
            resolve_knowledge_decision(&payload(0.77, true)).state,
            "shadow"
        );
        assert_eq!(
            resolve_knowledge_decision(&payload(0.91, false)).state,
            "shadow"
        );
        assert_eq!(
            resolve_knowledge_decision(&payload(0.61, true)).state,
            "timeline_only"
        );
    }

    #[test]
    fn test_collect_source_capture_ids_includes_new_captures_on_same_timeline() {
        let service = make_service();
        let primary = seed_capture(&service, 1_710_000_000_000, "Code", "主采集");
        let appended = seed_capture(&service, 1_710_000_010_000, "Code", "新增采集");
        let timeline_id = seed_knowledge(&service, "meeting", primary, 4, 2);
        link_captures_to_timeline(&service, timeline_id, &[primary, appended]);

        let candidate = make_candidate(&service, timeline_id);
        let ids = collect_source_capture_id_strings(&service.storage, &candidate)
            .expect("收集 source_capture_ids 失败");

        assert!(ids.contains(&primary.to_string()));
        assert!(ids.contains(&appended.to_string()));
    }

    #[test]
    fn test_refresh_document_source_metadata_backfills_url_without_sidecar() {
        let service = make_service();
        let primary = seed_capture(
            &service,
            1_710_000_000_000,
            "ChatGPT Atlas",
            "容器云 GPU 指标采集项目 - 云文档",
        );
        let appended = seed_capture(
            &service,
            1_710_000_010_000,
            "Google Chrome",
            "容器云 GPU 指标采集项目 - 云文档 - Google Chrome",
        );
        let timeline_id = seed_knowledge(&service, "document", primary, 4, 2);
        link_captures_to_timeline(&service, timeline_id, &[primary, appended]);

        let document_id = service
            .storage
            .insert_bake_document(&NewBakeDocument {
                title: "容器云 GPU 指标采集项目 - 常驻采集 GPU 利用率指标定义".to_string(),
                doc_type: "技术文档".to_string(),
                status: "enabled".to_string(),
                tags: "[]".to_string(),
                applicable_tasks: "[]".to_string(),
                source_memory_ids: to_json_string(&vec![timeline_id.to_string()]).unwrap(),
                source_capture_ids: to_json_string(&vec![primary.to_string()]).unwrap(),
                source_episode_ids: to_json_string(&vec![timeline_id.to_string()]).unwrap(),
                linked_knowledge_ids: to_json_string(&vec![timeline_id.to_string()]).unwrap(),
                sections_json: "[]".to_string(),
                style_phrases: "[]".to_string(),
                replacement_rules: "[]".to_string(),
                summary: None,
                full_content: Some("GPU 指标定义".to_string()),
                structured_content: "{}".to_string(),
                prompt_hint: None,
                diagram_code: None,
                image_assets: "[]".to_string(),
                source_app_name: Some("ChatGPT Atlas".to_string()),
                source_win_title: Some("容器云 GPU 指标采集项目 - 云文档".to_string()),
                source_url: None,
                content_hash: None,
                language: None,
                usage_count: 0,
                match_score: Some(0.75),
                match_level: Some("high".to_string()),
                creation_mode: "llm_bake".to_string(),
                review_status: "auto_created".to_string(),
                evidence_summary: None,
                generation_version: Some(BAKE_GENERATION_VERSION.to_string()),
                deleted_at: None,
            })
            .unwrap();

        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url = Some("https://docs.example.com/d/home/sample-document".to_string());
        let existing = service
            .storage
            .get_bake_document(document_id)
            .unwrap()
            .unwrap();

        assert!(service
            .refresh_document_source_metadata(&candidate, &existing)
            .unwrap());

        let updated = service
            .storage
            .get_bake_document(document_id)
            .unwrap()
            .unwrap();
        assert_eq!(
            updated.source_url.as_deref(),
            Some("https://docs.example.com/d/home/sample-document")
        );
        let source_capture_ids = parse_json_vec_string(&updated.source_capture_ids);
        assert!(source_capture_ids.contains(&primary.to_string()));
        assert!(source_capture_ids.contains(&appended.to_string()));
    }

    #[test]
    fn test_coalesce_skipped_candidate_registers_into_existing_document() {
        let service = make_service();
        let primary = seed_capture(
            &service,
            1_710_000_100_000,
            "Google Chrome",
            "项目周报 - 云文档",
        );
        let timeline_id = seed_knowledge(&service, "document", primary, 4, 2);
        link_captures_to_timeline(&service, timeline_id, &[primary]);

        let document_url = "https://docs.example.com/k/home/space/weekly-report";
        let document_id = service
            .storage
            .insert_bake_document(&NewBakeDocument {
                title: "项目周报".to_string(),
                doc_type: "周报".to_string(),
                status: "enabled".to_string(),
                tags: "[]".to_string(),
                applicable_tasks: "[]".to_string(),
                source_memory_ids: "[]".to_string(),
                source_capture_ids: "[]".to_string(),
                source_episode_ids: "[]".to_string(),
                linked_knowledge_ids: "[]".to_string(),
                sections_json: "[]".to_string(),
                style_phrases: "[]".to_string(),
                replacement_rules: "[]".to_string(),
                summary: None,
                full_content: Some("周报内容".to_string()),
                structured_content: "{}".to_string(),
                prompt_hint: None,
                diagram_code: None,
                image_assets: "[]".to_string(),
                source_app_name: None,
                source_win_title: None,
                source_url: Some(document_url.to_string()),
                content_hash: None,
                language: None,
                usage_count: 0,
                match_score: None,
                match_level: None,
                creation_mode: "llm_bake".to_string(),
                review_status: "auto_created".to_string(),
                evidence_summary: None,
                generation_version: Some(BAKE_GENERATION_VERSION.to_string()),
                deleted_at: None,
            })
            .unwrap();

        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url = Some(format!("{document_url}?from=home"));

        // URL 带 query 变体也应命中已有文档并立即登记，不进延迟列表
        let mut deferred: Vec<BakeMemorySourceRecord> = Vec::new();
        assert!(service
            .register_skipped_document_candidate_source(&candidate, document_url, &mut deferred)
            .unwrap());
        assert!(deferred.is_empty());

        let updated = service
            .storage
            .get_bake_document(document_id)
            .unwrap()
            .unwrap();
        let source_memory_ids = parse_json_vec_string(&updated.source_memory_ids);
        assert!(source_memory_ids.contains(&timeline_id.to_string()));
        let source_capture_ids = parse_json_vec_string(&updated.source_capture_ids);
        assert!(source_capture_ids.contains(&primary.to_string()));
    }

    #[test]
    fn test_coalesce_skipped_candidate_deferred_when_document_missing() {
        let service = make_service();
        let primary = seed_capture(&service, 1_710_000_200_000, "Google Chrome", "新建云文档");
        let timeline_id = seed_knowledge(&service, "document", primary, 4, 2);
        link_captures_to_timeline(&service, timeline_id, &[primary]);
        let mut candidate = make_candidate(&service, timeline_id);
        candidate.capture_url =
            Some("https://docs.example.com/k/home/space/new-document".to_string());

        let mut deferred: Vec<BakeMemorySourceRecord> = Vec::new();
        assert!(!service
            .register_skipped_document_candidate_source(
                &candidate,
                "https://docs.example.com/k/home/space/new-document",
                &mut deferred,
            )
            .unwrap());
        assert_eq!(deferred.len(), 1);
        assert_eq!(deferred[0].timeline.id, timeline_id);
    }

    #[test]
    fn test_build_knowledge_title_uses_overview_and_source_capture_ids() {
        let service = make_service();
        let primary = seed_capture(&service, 1_710_000_000_000, "Code", "主采集");
        let timeline_id = seed_knowledge(&service, "meeting", primary, 4, 2);
        link_captures_to_timeline(&service, timeline_id, &[primary]);
        let candidate = make_candidate(&service, timeline_id);
        let source_capture_ids = collect_source_capture_id_strings(&service.storage, &candidate)
            .expect("收集 source_capture_ids 失败");

        let payload = BakeKnowledgeArtifactPayload {
            summary: "时间线式标题".to_string(),
            overview: Some("这是提炼后的知识概述".to_string()),
            details: Some("知识详情".to_string()),
            entities: vec!["SGLang".to_string()],
            importance: Some(4),
            occurrence_count: None,
            observed_at: None,
            event_time_start: None,
            event_time_end: None,
            history_view: None,
            content_origin: None,
            activity_type: None,
            evidence_strength: None,
            evidence_summary: None,
            future_question: Some("未来应参考什么知识？".to_string()),
            decision_reason: Some("该事实对后续执行有直接参考价值".to_string()),
            match_score: Some(0.9),
            match_level: Some("high".to_string()),
            review_status: Some("auto_created".to_string()),
        };

        let record = build_bake_knowledge_entry(
            &candidate,
            &payload,
            "auto_created",
            "test",
            &source_capture_ids,
        )
        .expect("构建知识失败");

        assert_eq!(record.title, "这是提炼后的知识概述");
        assert_eq!(record.summary, "这是提炼后的知识概述");
        assert_eq!(
            parse_optional_json_vec_string(&record.source_capture_ids),
            vec![primary.to_string()]
        );
    }

    #[test]
    fn test_existing_knowledge_is_merged_with_new_timeline_captures() {
        let service = make_service();
        let primary = seed_capture(&service, 1_710_000_000_000, "Code", "主采集");
        let appended = seed_capture(&service, 1_710_000_010_000, "Code", "新增采集");
        let timeline_id = seed_knowledge(&service, "meeting", primary, 4, 2);
        link_captures_to_timeline(&service, timeline_id, &[primary, appended]);
        let existing_id = service
            .storage
            .insert_bake_knowledge(&NewBakeKnowledge {
                timeline_id,
                title: "旧知识".to_string(),
                summary: "旧摘要".to_string(),
                content: Some(r#"{"status":"auto_created"}"#.to_string()),
                detailed_content: Some("旧详情".to_string()),
                entities: r#"["旧实体"]"#.to_string(),
                importance: 3,
                source_capture_ids: Some(to_json_string(&vec![primary.to_string()]).unwrap()),
            })
            .expect("插入旧知识失败");
        let candidate = make_candidate(&service, timeline_id);
        let extraction = BakeArtifactExtraction {
            accepted: true,
            reason: None,
            payload: Some(json!({
                "summary": "新知识标题",
                "overview": "新知识概述",
                "details": "新详情",
                "entities": ["新实体"],
                "importance": 5,
                "evidence_summary": "来源记录了知识更新内容",
                "future_question": "后续应采用哪版知识？",
                "decision_reason": "新内容会直接影响后续执行",
                "match_score": 0.91,
                "match_level": "high",
                "review_status": "auto_created"
            })),
        };
        let mut existing_sources = std::collections::HashSet::from([timeline_id]);

        let result = service
            .persist_knowledge_artifact(
                None,
                &candidate,
                "test",
                &extraction,
                &mut existing_sources,
            )
            .expect("合并知识失败");

        assert_eq!(result.knowledge_created_count, 0);
        assert_eq!(service.storage.count_bake_knowledge().unwrap(), 1);
        let updated = service
            .storage
            .get_bake_knowledge(existing_id)
            .unwrap()
            .unwrap();
        assert_eq!(updated.title, "新知识概述");
        assert_eq!(updated.importance, 5);
        let source_ids = parse_optional_json_vec_string(&updated.source_capture_ids);
        assert!(source_ids.contains(&primary.to_string()));
        assert!(source_ids.contains(&appended.to_string()));
        let details = updated.detailed_content.unwrap();
        assert!(details.contains("旧详情"));
        assert!(details.contains("新详情"));
    }

    #[test]
    fn test_same_source_across_timelines_reuses_knowledge_and_sop_artifacts() {
        let service = make_service();
        let first_capture = seed_capture(&service, 1_710_000_000_000, "Code", "第一次采集");
        let second_capture = seed_capture(&service, 1_710_000_010_000, "Code", "第二次采集");
        let first_timeline = seed_knowledge(&service, "meeting", first_capture, 4, 1);
        let second_timeline = seed_knowledge(&service, "meeting", second_capture, 4, 1);
        let first_followup = seed_capture(&service, 1_710_000_001_000, "Code", "第一次验证");
        let second_followup = seed_capture(&service, 1_710_000_011_000, "Code", "第二次验证");
        let first_result = seed_capture(&service, 1_710_000_002_000, "Code", "第一次完成");
        let second_result = seed_capture(&service, 1_710_000_012_000, "Code", "第二次完成");
        link_captures_to_timeline(
            &service,
            first_timeline,
            &[first_capture, first_followup, first_result],
        );
        link_captures_to_timeline(
            &service,
            second_timeline,
            &[second_capture, second_followup, second_result],
        );
        let mut first = make_candidate(&service, first_timeline);
        first.action_trace = operation_trace(&[first_capture, first_followup, first_result]);
        let mut second = make_candidate(&service, second_timeline);
        second.action_trace = operation_trace(&[second_capture, second_followup, second_result]);

        let knowledge_extraction = BakeArtifactExtraction {
            accepted: true,
            reason: None,
            payload: Some(json!({
                "summary": "可复用知识",
                "overview": "相同来源只保留一条知识",
                "details": "知识详情",
                "entities": ["判重"],
                "importance": 4,
                "evidence_summary": "两条时间线来自同一事实来源",
                "future_question": "该来源已经沉淀了什么知识？",
                "decision_reason": "可避免重复沉淀并支持后续执行",
                "match_score": 0.9
            })),
        };
        let sop_extraction = BakeArtifactExtraction {
            accepted: true,
            reason: None,
            payload: Some(json!({
                "summary": "可复用 SOP",
                "overview": "相同来源只保留一条 SOP",
                "details": "SOP 详情",
                "steps": ["识别来源", "复用已有产物", "验证复用结果"],
                "step_evidence": [
                    {"step_index": 1, "capture_ids": [first_capture.to_string()]},
                    {"step_index": 2, "capture_ids": [first_followup.to_string()]},
                    {"step_index": 3, "capture_ids": [first_result.to_string()]}
                ],
                "linked_knowledge_ids": []
            })),
        };

        let mut knowledge_sources = std::collections::HashSet::new();
        service
            .persist_knowledge_artifact(
                None,
                &first,
                "test",
                &knowledge_extraction,
                &mut knowledge_sources,
            )
            .unwrap();
        let duplicate_knowledge = service
            .persist_knowledge_artifact(
                None,
                &second,
                "test",
                &knowledge_extraction,
                &mut knowledge_sources,
            )
            .unwrap();

        let mut sop_sources = std::collections::HashSet::new();
        service
            .persist_sop_artifact(None, &first, "test", &sop_extraction, &mut sop_sources)
            .unwrap();
        let duplicate_sop = service
            .persist_sop_artifact(None, &second, "test", &sop_extraction, &mut sop_sources)
            .unwrap();

        assert_eq!(service.storage.count_bake_knowledge().unwrap(), 1);
        assert_eq!(service.storage.count_bake_sops().unwrap(), 1);
        let sop_id = service
            .storage
            .find_bake_artifact_by_source_timeline("sop", first_timeline)
            .unwrap()
            .unwrap();
        let sop = service.storage.get_bake_sop(sop_id).unwrap().unwrap();
        let sop_content = parse_details(sop.content.as_deref());
        assert_eq!(
            sop_content
                .get("step_evidence_mode")
                .and_then(Value::as_str),
            Some("model_aligned")
        );
        assert_eq!(
            sop_content
                .get("step_evidence")
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(3)
        );
        assert!(sop
            .detailed_content
            .as_deref()
            .is_some_and(|content| content.contains("## 行动路线")));
        assert_eq!(duplicate_knowledge.discarded_count, 1);
        assert_eq!(duplicate_sop.discarded_count, 1);
        assert!(service
            .storage
            .find_existing_knowledge_timeline_ids(&[second_timeline])
            .unwrap()
            .contains(&second_timeline));
        assert!(service
            .storage
            .find_existing_sop_timeline_ids(&[second_timeline])
            .unwrap()
            .contains(&second_timeline));
    }

    fn sop_filter_with_query(query: &str) -> BakeListFilter {
        BakeListFilter {
            q: Some(query.to_string()),
            bucket: None,
            from_ts: None,
            to_ts: None,
            favorite: None,
            limit: 10,
            offset: 0,
            sort: BakeListSort::Recent,
        }
    }

    fn seed_sops(service: &BakeService) -> i64 {
        let capture_id = seed_capture(service, 1_710_000_000_000, "Chrome", "SOP 页面");
        // insert_timeline_entry 对 bake_sop 返回 bake_sops 行 id；
        // SOP 列表的可搜索字段来自 bake_sops 表，因此直接更新 bake_sops 行
        let hit_sop_id = seed_knowledge(service, CATEGORY_BAKE_SOP, capture_id, 3, 1);
        let other_sop_id = seed_knowledge(service, CATEGORY_BAKE_SOP, capture_id, 3, 1);
        service
            .storage
            .with_conn(|conn| {
                conn.execute(
                    "UPDATE bake_sops SET title = '无关概览', summary = '无关摘要', content = '{}'
                     WHERE id = ?1",
                    rusqlite::params![other_sop_id],
                )?;
                Ok(())
            })
            .unwrap();
        hit_sop_id
    }

    #[test]
    fn test_list_sops_paginated_query_prefilter_results_match_contains() {
        let service = make_service();
        let hit_id = seed_sops(&service);

        let page = service
            .list_sops_paginated(sop_filter_with_query("知识摘要"))
            .unwrap();
        assert_eq!(page.total, 1);
        assert_eq!(page.items.len(), 1);
        assert_eq!(page.items[0].id, hit_id.to_string());
    }

    #[test]
    fn test_list_sops_paginated_query_falls_back_without_fts() {
        let service = make_service();
        let hit_id = seed_sops(&service);
        service
            .storage
            .with_conn(|conn| {
                conn.execute_batch(
                    "DROP TRIGGER IF EXISTS bake_sops_fts_insert;
                     DROP TRIGGER IF EXISTS bake_sops_fts_update;
                     DROP TRIGGER IF EXISTS bake_sops_fts_delete;
                     DROP TABLE IF EXISTS bake_sops_fts;",
                )?;
                Ok(())
            })
            .unwrap();

        let page = service
            .list_sops_paginated(sop_filter_with_query("知识摘要"))
            .unwrap();
        assert_eq!(page.total, 1);
        assert_eq!(page.items.len(), 1);
        assert_eq!(page.items[0].id, hit_id.to_string());
    }
}
