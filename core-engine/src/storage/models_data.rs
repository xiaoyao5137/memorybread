use serde::{Deserialize, Serialize};
use serde_json::Value;

/// 模型分类发现的数据页面注册结果。
///
/// 时间线提炼的同一次推理可能输出 data_pages 分类，sidecar 校验后调用注册接口；
/// 非法 URL、缺失或敏感的 capture 都直接拒绝，不创建数据源。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DiscoveredSourceOutcome {
    Registered { source_id: i64, created: bool },
    RejectedInvalidUrl,
    RejectedNotRefreshable,
    RejectedCaptureMissing,
    RejectedCaptureSensitive,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DataSnapshotRecord {
    pub id: i64,
    pub source_id: i64,
    pub collected_at: i64,
    pub observed_at: Option<i64>,
    pub collector: String,
    pub content_text: String,
    pub structured_data: Value,
    pub content_hash: String,
    pub freshness_ttl_seconds: i64,
    pub provenance: Value,
    pub source_capture_ids: Vec<i64>,
    pub source_timeline_ids: Vec<i64>,
    pub status: String,
    pub period_granularity: String,
    pub period_key: String,
    pub period_start_at: Option<i64>,
    pub period_end_at: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DataSourceRecord {
    pub id: i64,
    pub title: String,
    pub source_kind: String,
    pub source_url: Option<String>,
    pub access_mode: String,
    pub refresh_policy: String,
    pub realtime_level: String,
    pub source_app_name: Option<String>,
    pub source_window_title: Option<String>,
    pub tags: Vec<String>,
    pub first_seen_at: i64,
    pub last_seen_at: i64,
    pub last_collected_at: Option<i64>,
    pub last_success_at: Option<i64>,
    pub last_error_code: Option<String>,
    pub status: String,
    pub created_at: i64,
    pub updated_at: i64,
    pub latest_snapshot: Option<DataSnapshotRecord>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DataSearchResult {
    pub source_id: i64,
    pub title: String,
    pub source_kind: String,
    pub source_url: Option<String>,
    pub access_mode: String,
    pub refresh_policy: String,
    pub observed_at: Option<i64>,
    pub collected_at: Option<i64>,
    pub freshness_class: String,
    pub freshness_score: f64,
    /// 来源标题/URL 与当前查询的身份匹配分；不受历史采集正文影响。
    pub identity_relevance_score: f64,
    pub relevance_score: f64,
    pub final_score: f64,
    pub refresh_required: bool,
    pub can_use: bool,
    pub content_excerpt: Option<String>,
    pub structured_data: Option<Value>,
    pub provenance: Option<Value>,
    /// 同一语义数据源按阶段保留的历史快照，最新阶段在前。
    pub history: Vec<DataSnapshotRecord>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DataExtractionSummary {
    pub scanned_count: usize,
    pub source_created_count: usize,
    pub source_updated_count: usize,
    pub snapshot_created_count: usize,
    pub historical_regenerated_count: usize,
    pub historical_merged_count: usize,
    pub historical_rejected_count: usize,
    pub skipped_count: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreationEvidenceAssetRecord {
    pub id: String,
    pub run_id: String,
    pub session_id: String,
    pub history_id: Option<i64>,
    pub source_id: Option<i64>,
    pub data_snapshot_id: Option<i64>,
    pub source_url: String,
    pub page_title: String,
    pub captured_at: i64,
    #[serde(skip_serializing)]
    pub image_path: String,
    pub mime_type: String,
    pub width: i64,
    pub height: i64,
    pub content_hash: String,
    pub screenshot_source: String,
    pub validation_status: String,
    pub validation: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreationEvidenceAssetView {
    pub id: String,
    pub run_id: String,
    pub session_id: String,
    pub history_id: Option<i64>,
    pub source_id: Option<i64>,
    pub data_snapshot_id: Option<i64>,
    pub source_url: String,
    pub page_title: String,
    pub captured_at: i64,
    pub image_url: String,
    pub mime_type: String,
    pub width: i64,
    pub height: i64,
    pub content_hash: String,
    pub screenshot_source: String,
    pub validation_status: String,
    pub validation: Value,
}

impl From<CreationEvidenceAssetRecord> for CreationEvidenceAssetView {
    fn from(record: CreationEvidenceAssetRecord) -> Self {
        let image_url = format!("/api/creation/evidence/{}/image", record.id);
        Self {
            id: record.id,
            run_id: record.run_id,
            session_id: record.session_id,
            history_id: record.history_id,
            source_id: record.source_id,
            data_snapshot_id: record.data_snapshot_id,
            source_url: record.source_url,
            page_title: record.page_title,
            captured_at: record.captured_at,
            image_url,
            mime_type: record.mime_type,
            width: record.width,
            height: record.height,
            content_hash: record.content_hash,
            screenshot_source: record.screenshot_source,
            validation_status: record.validation_status,
            validation: record.validation,
        }
    }
}
