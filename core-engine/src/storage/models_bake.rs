use serde::{Deserialize, Serialize};

use crate::storage::db::current_ts_ms;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeDocumentRecord {
    pub id: i64,
    pub title: String,
    pub doc_type: String,
    pub status: String,
    pub tags: String,
    pub applicable_tasks: String,
    pub source_memory_ids: String,
    pub source_capture_ids: String,
    pub source_episode_ids: String,
    pub linked_knowledge_ids: String,
    pub sections_json: String,
    pub style_phrases: String,
    pub replacement_rules: String,
    pub summary: Option<String>,
    pub full_content: Option<String>,
    pub structured_content: String,
    pub prompt_hint: Option<String>,
    pub diagram_code: Option<String>,
    pub image_assets: String,
    pub source_app_name: Option<String>,
    pub source_win_title: Option<String>,
    pub source_url: Option<String>,
    pub content_hash: Option<String>,
    pub language: Option<String>,
    pub usage_count: i64,
    pub match_score: Option<f64>,
    pub match_level: Option<String>,
    pub creation_mode: String,
    pub review_status: String,
    pub evidence_summary: Option<String>,
    pub generation_version: Option<String>,
    pub deleted_at: Option<i64>,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NewBakeDocument {
    pub title: String,
    pub doc_type: String,
    pub status: String,
    pub tags: String,
    pub applicable_tasks: String,
    pub source_memory_ids: String,
    pub source_capture_ids: String,
    pub source_episode_ids: String,
    pub linked_knowledge_ids: String,
    pub sections_json: String,
    pub style_phrases: String,
    pub replacement_rules: String,
    pub summary: Option<String>,
    pub full_content: Option<String>,
    pub structured_content: String,
    pub prompt_hint: Option<String>,
    pub diagram_code: Option<String>,
    pub image_assets: String,
    pub source_app_name: Option<String>,
    pub source_win_title: Option<String>,
    pub source_url: Option<String>,
    pub content_hash: Option<String>,
    pub language: Option<String>,
    pub usage_count: i64,
    pub match_score: Option<f64>,
    pub match_level: Option<String>,
    pub creation_mode: String,
    pub review_status: String,
    pub evidence_summary: Option<String>,
    pub generation_version: Option<String>,
    pub deleted_at: Option<i64>,
}

impl NewBakeDocument {
    pub fn with_defaults(title: String, doc_type: String) -> Self {
        Self {
            title,
            doc_type,
            status: "draft".to_string(),
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
            full_content: None,
            structured_content: "{}".to_string(),
            prompt_hint: None,
            diagram_code: None,
            image_assets: "[]".to_string(),
            source_app_name: None,
            source_win_title: None,
            source_url: None,
            content_hash: None,
            language: None,
            usage_count: 0,
            match_score: None,
            match_level: None,
            creation_mode: "manual".to_string(),
            review_status: "draft".to_string(),
            evidence_summary: None,
            generation_version: None,
            deleted_at: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
// ─────────────────────────────────────────────────────────────────────────────
// timelines 表 - 时间线（原 episodic_memories）
// ─────────────────────────────────────────────────────────────────────────────

pub struct NewTimeline {
    pub capture_id: i64,
    pub summary: String,
    pub overview: Option<String>,
    pub details: Option<String>,
    pub entities: String,
    pub category: String,
    pub importance: i64,
    pub occurrence_count: Option<i64>,
    pub observed_at: Option<i64>,
    pub event_time_start: Option<i64>,
    pub event_time_end: Option<i64>,
    pub history_view: bool,
    pub content_origin: Option<String>,
    pub activity_type: Option<String>,
    pub is_self_generated: bool,
    pub evidence_strength: Option<String>,
    pub capture_ids: Option<String>,
    pub start_time: Option<i64>,
    pub end_time: Option<i64>,
    pub duration_minutes: Option<i64>,
    pub frag_app_name: Option<String>,
    pub frag_win_title: Option<String>,
    pub time_range_start: Option<i64>,
    pub time_range_end: Option<i64>,
    pub key_timestamps: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TimelineRecord {
    pub id: i64,
    pub capture_id: i64,
    pub summary: String,
    pub overview: Option<String>,
    pub details: Option<String>,
    pub detailed_content: Option<String>,
    pub entities: String,
    pub category: String,
    pub importance: i64,
    pub occurrence_count: Option<i64>,
    pub observed_at: Option<i64>,
    pub event_time_start: Option<i64>,
    pub event_time_end: Option<i64>,
    pub history_view: bool,
    pub content_origin: Option<String>,
    pub activity_type: Option<String>,
    pub is_self_generated: bool,
    pub evidence_strength: Option<String>,
    pub user_verified: bool,
    pub user_edited: bool,
    pub created_at: String,
    pub updated_at: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub capture_ids: Option<String>,
    pub start_time: Option<i64>,
    pub end_time: Option<i64>,
    pub duration_minutes: Option<i64>,
    pub frag_app_name: Option<String>,
    pub frag_win_title: Option<String>,
    pub time_range_start: Option<i64>,
    pub time_range_end: Option<i64>,
    pub key_timestamps: Option<String>,
}

// 向后兼容的类型别名
pub type NewEpisodicMemory = NewTimeline;
pub type EpisodicMemoryRecord = TimelineRecord;

// ─────────────────────────────────────────────────────────────────────────────
// bake_knowledge 表 - 提炼后的知识
// ─────────────────────────────────────────────────────────────────────────────

pub struct NewBakeKnowledge {
    pub timeline_id: i64,
    pub title: String,
    pub summary: String,
    pub content: Option<String>,
    pub detailed_content: Option<String>,
    pub entities: String,
    pub importance: i64,
    pub source_capture_ids: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeKnowledgeRecord {
    pub id: i64,
    pub timeline_id: i64,
    pub title: String,
    pub summary: String,
    pub content: Option<String>,
    pub detailed_content: Option<String>,
    pub entities: String,
    pub importance: i64,
    pub user_verified: bool,
    pub user_edited: bool,
    pub created_at: String,
    pub updated_at: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub source_capture_ids: Option<String>,
    pub occurrence_count: i64,
}

// ─────────────────────────────────────────────────────────────────────────────
// bake_sops 表 - 提炼后的操作手册
// ─────────────────────────────────────────────────────────────────────────────

pub struct NewBakeSop {
    pub timeline_id: i64,
    pub title: String,
    pub summary: String,
    pub content: Option<String>,
    pub detailed_content: Option<String>,
    pub entities: String,
    pub importance: i64,
    pub source_capture_ids: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeSopRecord {
    pub id: i64,
    pub timeline_id: i64,
    pub title: String,
    pub summary: String,
    pub content: Option<String>,
    pub detailed_content: Option<String>,
    pub entities: String,
    pub importance: i64,
    pub user_verified: bool,
    pub user_edited: bool,
    pub created_at: String,
    pub updated_at: String,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
    pub source_capture_ids: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeMemorySourceRecord {
    pub timeline: TimelineRecord,
    pub capture_ts: i64,
    pub capture_app_name: Option<String>,
    pub capture_win_title: Option<String>,
    pub capture_ax_text: Option<String>,
    pub capture_ocr_text: Option<String>,
    pub capture_input_text: Option<String>,
    pub capture_audio_text: Option<String>,
    pub capture_url: Option<String>,
    pub capture_webpage_title: Option<String>,
    /// 从时间线全部成员 capture 中按出现次数与时间选出的可靠文档标题。
    /// 主 capture 可能仍停留在“知识库/未命名文档”等加载占位页。
    #[serde(default)]
    pub preferred_source_title: Option<String>,
    pub url_aggregated_text: Option<String>,
    pub url_aggregated_capture_count: i64,
    /// 同一 timeline 内严格按时间排序的采集轨迹。与面向文档还原的聚合正文分离，
    /// 不按页面开头去重，避免丢失操作前后状态。
    #[serde(default)]
    pub action_trace: Vec<BakeActionTraceRecord>,
    /// 0 表示 fresh lane；大于 0 表示独立于 watermark 的 retry lane。
    #[serde(default)]
    pub retry_failure_count: i64,
    #[serde(default)]
    pub retry_error_code: Option<String>,
    #[serde(default)]
    pub retry_next_at_ms: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeActionTraceRecord {
    pub capture_id: i64,
    pub ts: i64,
    pub event_type: String,
    pub app_name: Option<String>,
    pub win_title: Option<String>,
    pub url: Option<String>,
    pub webpage_title: Option<String>,
    pub visible_text: Option<String>,
    pub input_text: Option<String>,
    pub audio_text: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeActivityRecord {
    pub message: String,
    pub ts: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeOverviewRecord {
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
    pub recent_activities: Vec<BakeActivityRecord>,
}

impl BakeOverviewRecord {
    pub fn empty() -> Self {
        Self {
            capture_count: 0,
            memory_count: 0,
            data_count: 0,
            knowledge_count: 0,
            template_count: 0,
            sop_count: 0,
            pending_candidates: 0,
            auto_created_today: 0,
            candidate_today: 0,
            discarded_today: 0,
            last_bake_run_status: None,
            last_bake_run_at: None,
            last_trigger_reason: None,
            knowledge_auto_count: 0,
            template_auto_count: 0,
            sop_auto_count: 0,
            recent_activities: Vec::new(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NewBakeRun {
    pub trigger_reason: String,
    pub status: String,
    pub started_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeRunRecord {
    pub id: i64,
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
pub struct BakeWatermarkRecord {
    pub pipeline_name: String,
    pub last_processed_ts: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeRetryStateRecord {
    pub timeline_id: i64,
    pub failure_count: i64,
    pub last_error: Option<String>,
    pub last_error_code: Option<String>,
    pub last_failed_at_ms: i64,
    pub next_retry_at_ms: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BakeCandidateAuditRecord {
    pub id: i64,
    pub run_id: i64,
    pub timeline_id: i64,
    pub lane: String,
    pub source_capture_count: i64,
    pub effective_capture_count: i64,
    pub sop_eligible: bool,
    pub sop_eligibility_reason: Option<String>,
    pub primary_type: Option<String>,
    pub classification_reason: Option<String>,
    pub sop_model_accepted: Option<bool>,
    pub sop_model_reason: Option<String>,
    pub sop_payload_valid: Option<bool>,
    pub persist_status: String,
    pub persist_reason: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone)]
pub struct NewBakeCandidateAudit {
    pub run_id: i64,
    pub timeline_id: i64,
    pub lane: String,
    pub source_capture_count: i64,
    pub effective_capture_count: i64,
    pub sop_eligible: bool,
    pub sop_eligibility_reason: Option<String>,
    pub persist_status: String,
    pub persist_reason: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BakeSopFunnelSummaryRecord {
    pub audited_count: i64,
    pub eligible_count: i64,
    pub model_accepted_count: i64,
    pub payload_valid_count: i64,
    pub persisted_count: i64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BakeQueueStatusRecord {
    pub watermark_last_processed_ts: i64,
    pub watermark_updated_at_ms: Option<i64>,
    pub fresh_count: i64,
    pub metadata_refresh_count: i64,
    pub retry_ready_count: i64,
    pub retry_delayed_count: i64,
    pub dead_letter_count: i64,
    pub retry_timeout_count: i64,
    pub retry_output_count: i64,
    pub retry_upstream_count: i64,
    pub retry_other_count: i64,
    pub actionable_count: i64,
    pub pending_count: i64,
    pub oldest_fresh_at_ms: Option<i64>,
    pub oldest_retry_at_ms: Option<i64>,
    pub oldest_actionable_at_ms: Option<i64>,
    pub next_retry_at_ms: Option<i64>,
    pub recent_no_progress_count: i64,
    pub recommended_retry_after_ms: i64,
}

pub fn now_ms() -> i64 {
    current_ts_ms()
}
