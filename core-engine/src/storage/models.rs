//! 与数据库表一一对应的 Rust 数据模型
//!
//! 命名规范：
//! - `XxxRecord`  — 从数据库读出的完整行（含 id）
//! - `NewXxx`     — 插入时用的参数结构体（不含 id/ts 等自动生成字段）

use serde::{Deserialize, Serialize};

// ─────────────────────────────────────────────────────────────────────────────
// captures 表
// ─────────────────────────────────────────────────────────────────────────────

/// 触发采集的事件类型
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventType {
    AppSwitch,
    BrowserNavigation,
    MouseClick,
    Scroll,
    KeyPause,
    Manual,
    Auto,
}

impl EventType {
    pub fn as_str(&self) -> &'static str {
        match self {
            EventType::AppSwitch => "app_switch",
            EventType::BrowserNavigation => "browser_navigation",
            EventType::MouseClick => "mouse_click",
            EventType::Scroll => "scroll",
            EventType::KeyPause => "key_pause",
            EventType::Manual => "manual",
            EventType::Auto => "auto",
        }
    }
}

impl TryFrom<&str> for EventType {
    type Error = String;
    fn try_from(s: &str) -> Result<Self, Self::Error> {
        match s {
            "app_switch" => Ok(EventType::AppSwitch),
            "browser_navigation" => Ok(EventType::BrowserNavigation),
            "mouse_click" => Ok(EventType::MouseClick),
            "scroll" => Ok(EventType::Scroll),
            "key_pause" => Ok(EventType::KeyPause),
            "manual" => Ok(EventType::Manual),
            "auto" => Ok(EventType::Auto),
            other => Err(format!("未知事件类型: {other}")),
        }
    }
}

/// 从 captures 表读出的完整行
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaptureRecord {
    pub id: i64,
    pub ts: i64,
    pub app_name: Option<String>,
    pub app_bundle_id: Option<String>,
    pub win_title: Option<String>,
    pub event_type: String,
    /// OCR 之前通过程序化通道提取到的文本。
    ///
    /// 历史字段名为 `ax_text`，但实际语义更接近 `programmatic_text`：
    /// 可能来自 macOS Accessibility Tree，也可能来自浏览器 AppleScript
    /// fallback 执行的 DOM `innerText`，或其他应用专用文本提取器。
    pub ax_text: Option<String>,
    pub ax_focused_role: Option<String>,
    pub ax_focused_id: Option<String>,
    pub ocr_text: Option<String>,
    pub screenshot_path: Option<String>,
    pub screenshot_source: Option<String>,
    pub input_text: Option<String>,
    pub audio_text: Option<String>,
    pub is_sensitive: bool,
    pub pii_scrubbed: bool,
    pub url: Option<String>,
    pub webpage_title: Option<String>,
}

/// 工作画像页使用的采集活动聚合结果。
///
/// 只暴露日期、应用名与时长统计，不携带窗口标题或采集正文。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CaptureActivityAggregate {
    pub day_index: i64,
    pub app_name: String,
    pub duration_ms: i64,
    pub capture_count: i64,
    pub active_period_count: i64,
    pub first_ts: i64,
    pub last_ts: i64,
}

/// 工作画像内部用于心情推断的企业 IM 采集文本。
///
/// 文本来自已经过采集隐私门禁和内容脱敏的现有记录；该结构只在本地聚合流程中使用，
/// API 不返回原始文本。
#[derive(Debug, Clone)]
pub struct WorkImCaptureSample {
    pub app_name: String,
    pub text: String,
}

/// 按内置工作类别聚合的有效工作时长（毫秒）。
///
/// 分类规则只依赖应用 Bundle ID 与窗口标题关键词，全部在本机完成；
/// 该结构只携带聚合后的时长，不包含任何应用名、标题或正文。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct WorkCategoryTotals {
    pub coding_ms: i64,
    pub design_ms: i64,
    pub knowledge_ms: i64,
    pub focus_ms: i64,
}

impl CaptureRecord {
    /// 返回最佳文本（programmatic/ax_text 优先，fallback 到 ocr_text）
    pub fn best_text(&self) -> Option<&str> {
        self.ax_text.as_deref().or(self.ocr_text.as_deref())
    }
}

/// 插入 captures 时使用的参数
#[derive(Debug, Clone)]
pub struct NewCapture {
    pub ts: i64,
    pub app_name: Option<String>,
    pub app_bundle_id: Option<String>,
    pub win_title: Option<String>,
    pub event_type: EventType,
    /// OCR 之前通过程序化通道提取到的文本；字段名沿用历史命名 `ax_text`。
    pub ax_text: Option<String>,
    pub ax_focused_role: Option<String>,
    pub ax_focused_id: Option<String>,
    pub ocr_text: Option<String>,
    pub screenshot_path: Option<String>,
    pub screenshot_source: Option<String>,
    pub input_text: Option<String>,
    pub is_sensitive: bool,
    pub pii_scrubbed: bool,
    pub url: Option<String>,
    pub webpage_title: Option<String>,
}

/// 一次采集尝试的持久化审计记录。
///
/// 隐私跳过时调用方必须把 `app_name` / `win_title` 留空，只记录时间与原因。
#[derive(Debug, Clone)]
pub struct NewCaptureAttempt {
    pub observed_at: i64,
    pub event_type: String,
    pub outcome: String,
    pub reason: String,
    pub capture_id: Option<i64>,
    pub related_capture_id: Option<i64>,
    pub app_name: Option<String>,
    pub win_title: Option<String>,
    pub is_private: bool,
    pub effective_interval_secs: Option<u64>,
}

// ─────────────────────────────────────────────────────────────────────────────
// user_preferences 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PreferenceRecord {
    pub id: i64,
    pub key: String,
    pub value: String,
    pub source: String,
    pub confidence: f64,
    pub updated_at: i64,
    pub sample_count: i64,
}

// ─────────────────────────────────────────────────────────────────────────────
// action_logs 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ActionStatus {
    Pending,
    Success,
    Failed,
    Cancelled,
    Interrupted,
}

impl ActionStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ActionStatus::Pending => "pending",
            ActionStatus::Success => "success",
            ActionStatus::Failed => "failed",
            ActionStatus::Cancelled => "cancelled",
            ActionStatus::Interrupted => "interrupted",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActionLogRecord {
    pub id: i64,
    pub ts: i64,
    pub trigger_source: String,
    pub app_name: Option<String>,
    pub action_type: String,
    pub action_payload: String, // JSON 字符串
    pub confirmed_by_user: bool,
    pub status: String,
    pub user_correction: Option<String>,
    pub error_msg: Option<String>,
}

#[derive(Debug, Clone)]
pub struct NewActionLog {
    pub ts: i64,
    pub trigger_source: String,
    pub app_name: Option<String>,
    pub action_type: String,
    pub action_payload: String, // JSON 序列化后的字符串
    pub confirmed_by_user: bool,
}

// ─────────────────────────────────────────────────────────────────────────────
// style_samples 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StyleSampleRecord {
    pub id: i64,
    pub ts: i64,
    pub scene_type: String,
    pub content: String,
    pub app_name: Option<String>,
    pub quality: f64,
    pub word_count: i64,
}

#[derive(Debug, Clone)]
pub struct NewStyleSample {
    pub ts: i64,
    pub scene_type: String,
    pub content: String,
    pub app_name: Option<String>,
    pub quality: f64,
}

// ─────────────────────────────────────────────────────────────────────────────
// vector_index 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VectorIndexRecord {
    pub id: i64,
    pub capture_id: i64,
    pub qdrant_point_id: String,
    pub chunk_index: i64,
    pub chunk_text: String,
    pub model_name: String,
    pub created_at: i64,
    pub doc_key: String,
    pub source_type: String,
    pub knowledge_id: Option<i64>,
    pub time: Option<i64>,
    pub start_time: Option<i64>,
    pub end_time: Option<i64>,
    pub observed_at: Option<i64>,
    pub event_time_start: Option<i64>,
    pub event_time_end: Option<i64>,
    pub history_view: bool,
    pub content_origin: Option<String>,
    pub activity_type: Option<String>,
    pub is_self_generated: bool,
    pub evidence_strength: Option<String>,
    pub app_name: Option<String>,
    pub win_title: Option<String>,
    pub category: Option<String>,
    pub user_verified: bool,
}

#[derive(Debug, Clone)]
pub struct NewVectorIndex {
    pub capture_id: i64,
    pub qdrant_point_id: String,
    pub chunk_index: i64,
    pub chunk_text: String,
    pub model_name: String,
    pub created_at: i64,
    pub doc_key: String,
    pub source_type: String,
    pub knowledge_id: Option<i64>,
    pub time: Option<i64>,
    pub start_time: Option<i64>,
    pub end_time: Option<i64>,
    pub observed_at: Option<i64>,
    pub event_time_start: Option<i64>,
    pub event_time_end: Option<i64>,
    pub history_view: bool,
    pub content_origin: Option<String>,
    pub activity_type: Option<String>,
    pub is_self_generated: bool,
    pub evidence_strength: Option<String>,
    pub app_name: Option<String>,
    pub win_title: Option<String>,
    pub category: Option<String>,
    pub user_verified: bool,
}

// ─────────────────────────────────────────────────────────────────────────────
// rag_sessions 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RagSessionRecord {
    pub id: i64,
    pub ts: i64,
    pub scene_type: Option<String>,
    pub user_query: String,
    pub retrieved_ids: Option<String>, // JSON 数组字符串
    pub prompt_used: Option<String>,
    pub llm_response: Option<String>,
    pub user_feedback: Option<String>,
    pub latency_ms: Option<i64>,
    pub model: Option<String>,
}

#[derive(Debug, Clone)]
pub struct NewRagSession {
    pub ts: i64,
    pub scene_type: Option<String>,
    pub user_query: String,
    pub retrieved_ids: Option<String>,
    pub prompt_used: Option<String>,
    pub llm_response: Option<String>,
    pub latency_ms: Option<i64>,
    pub model: Option<String>,
}

// ─────────────────────────────────────────────────────────────────────────────
// app_blacklist 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppBlacklistRecord {
    pub id: i64,
    pub bundle_id: String,
    pub app_name: String,
    pub enabled: bool,
    pub reason: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone)]
pub struct NewAppBlacklist {
    pub bundle_id: String,
    pub app_name: String,
    pub enabled: bool,
    pub reason: Option<String>,
}

// ─────────────────────────────────────────────────────────────────────────────
// privacy_filters 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PrivacyFilterRecord {
    pub id: i64,
    pub filter_type: String,
    pub filter_name: String,
    pub enabled: bool,
    pub config_json: Option<String>,
    pub updated_at: String,
}

#[derive(Debug, Clone)]
pub struct NewPrivacyFilter {
    pub filter_type: String,
    pub filter_name: String,
    pub enabled: bool,
    pub config_json: Option<String>,
}

/// 隐私拦截统计记录
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PrivacyBlockStat {
    pub id: i64,
    pub stat_type: String,
    pub target_id: String,
    pub block_count: i64,
    pub week_start: String,
    pub updated_at: String,
}

// ─────────────────────────────────────────────────────────────────────────────
// user_profiles 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UserProfileRecord {
    pub id: i64,
    pub snapshot_type: String,
    pub snapshot_date: String,
    pub content: String,
    pub is_system_generated: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone)]
pub struct NewUserProfile {
    pub snapshot_type: String,
    pub snapshot_date: String,
    pub content: String,
    pub is_system_generated: bool,
}

// ─────────────────────────────────────────────────────────────────────────────
// diaries 表
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DiaryRecord {
    pub id: i64,
    pub period_type: String,
    pub period_start: String,
    pub period_end: String,
    pub diary_date: String,
    pub content: String,
    pub source_timeline_ids: String,
    pub source_diary_ids: String,
    pub generation_status: String,
    pub is_system_generated: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone)]
pub struct NewDiaryEntry {
    pub period_type: String,
    pub period_start: String,
    pub period_end: String,
    pub diary_date: String,
    pub content: String,
    pub source_timeline_ids: String,
    pub source_diary_ids: String,
    pub generation_status: String,
    pub is_system_generated: bool,
}
