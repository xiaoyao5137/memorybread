//! CaptureEngine — 核心采集引擎
//!
//! 协调截图、AX 信息抓取、隐私过滤和 SQLite 存储。
//!
//! 设计模式：
//! - 事件通过 `tokio::sync::mpsc::Receiver<CaptureEvent>` 注入
//! - 引擎本身不包含事件监听逻辑（由 `listener` 模块或外部注入）
//! - 这使得引擎在测试中可以完全脱离系统 API 运行

use std::collections::hash_map::DefaultHasher;
use std::collections::{HashMap, VecDeque};
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::sync::atomic::{AtomicI64, AtomicU64, AtomicUsize, Ordering};
use std::sync::OnceLock;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tokio::sync::{mpsc, Semaphore};
use tracing::{debug, error, info, warn};

use crate::ipc::IpcClient;
use crate::storage::{
    models::{EventType, NewCapture, NewCaptureAttempt, NewVectorIndex},
    StorageManager,
};

use super::{
    ax::{get_frontmost_context_snapshot_async, get_frontmost_info_async, AXInfo},
    blacklist::BlacklistChecker,
    content_filter::ContentFilter,
    filter::PrivacyFilter,
    screenshot::{
        capture_and_save_async, capture_visual_probe_async, hamming_distance,
        save_visual_probe_async, VisualDifference, VisualFingerprint, VisualProbe,
    },
    CaptureError,
};

const LOGINWINDOW_BUNDLE_ID: &str = "com.apple.loginwindow";
const LOGINWINDOW_APP_NAME: &str = "loginwindow";

fn is_system_session_context(bundle_id: Option<&str>, app_name: Option<&str>) -> bool {
    bundle_id.is_some_and(|value| value.trim().eq_ignore_ascii_case(LOGINWINDOW_BUNDLE_ID))
        || app_name.is_some_and(|value| value.trim().eq_ignore_ascii_case(LOGINWINDOW_APP_NAME))
}

// ─────────────────────────────────────────────────────────────────────────────
// CaptureConfig
// ─────────────────────────────────────────────────────────────────────────────

/// 采集引擎配置参数
#[derive(Debug, Clone)]
pub struct CaptureConfig {
    /// 截图根目录（绝对路径）
    pub captures_dir: PathBuf,
    /// JPEG 压缩质量 0–100（推荐 80）
    pub screenshot_quality: u8,
    /// 是否启用截图（可在低电量模式下关闭）
    pub enable_screenshot: bool,
    /// 是否启用 Accessibility 信息抓取
    pub enable_ax: bool,
}

impl Default for CaptureConfig {
    fn default() -> Self {
        let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
        Self {
            captures_dir: PathBuf::from(home).join(".memory-bread").join("captures"),
            screenshot_quality: 80,
            enable_screenshot: true,
            enable_ax: true,
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// CaptureEvent
// ─────────────────────────────────────────────────────────────────────────────

/// 触发一次采集的事件类型
#[derive(Debug, Clone)]
pub enum CaptureEvent {
    /// 前台应用发生切换（最高优先级）
    AppSwitch {
        app_name: String,
        bundle_id: Option<String>,
        win_title: String,
    },
    /// 浏览器前台标签页 URL 发生变化（触发完整 AX 采集，必要时截图并 OCR）
    BrowserNavigation {
        app_name: String,
        bundle_id: Option<String>,
        win_title: Option<String>,
        url: String,
        webpage_title: Option<String>,
    },
    /// 鼠标点击（在新位置落点）
    MouseClick { x: f64, y: f64 },
    /// 键盘活动后停顿。只表示发生过活动，不携带键码、字符或输入文本。
    KeyPause,
    /// 页面/内容滚动
    Scroll,
    /// 定时兜底采集（按配置触发，默认 90 秒）
    Periodic,
    /// 用户手动唤醒
    Manual,
}

impl CaptureEvent {
    /// 映射到数据库 event_type 字段。
    pub fn to_event_type(&self) -> EventType {
        match self {
            CaptureEvent::AppSwitch { .. } => EventType::AppSwitch,
            CaptureEvent::BrowserNavigation { .. } => EventType::BrowserNavigation,
            CaptureEvent::MouseClick { .. } => EventType::MouseClick,
            CaptureEvent::KeyPause => EventType::KeyPause,
            CaptureEvent::Scroll => EventType::Scroll,
            CaptureEvent::Periodic => EventType::Auto,
            CaptureEvent::Manual => EventType::Manual,
        }
    }

    /// 输入事件只作为触发信号，永不携带或保存原始按键文本。
    pub fn input_text(&self) -> Option<&str> {
        None
    }

    /// 提取事件携带的应用名（AppSwitch 专用）。
    pub fn app_name(&self) -> Option<&str> {
        match self {
            CaptureEvent::AppSwitch { app_name, .. }
            | CaptureEvent::BrowserNavigation { app_name, .. } => Some(app_name),
            _ => None,
        }
    }

    /// 变化监听器随事件携带的可信前台上下文。
    fn context_info(&self) -> Option<AXInfo> {
        match self {
            CaptureEvent::AppSwitch {
                app_name,
                bundle_id,
                win_title,
            } => Some(AXInfo {
                app_name: Some(app_name.clone()),
                app_bundle_id: bundle_id.clone(),
                win_title: (!win_title.trim().is_empty()).then(|| win_title.clone()),
                ..Default::default()
            }),
            CaptureEvent::BrowserNavigation {
                app_name,
                bundle_id,
                win_title,
                url,
                webpage_title,
            } => Some(AXInfo {
                app_name: Some(app_name.clone()),
                app_bundle_id: bundle_id.clone(),
                win_title: win_title.clone(),
                url: Some(url.clone()),
                webpage_title: webpage_title.clone(),
                ..Default::default()
            }),
            _ => None,
        }
    }

    fn is_context_change_event(&self) -> bool {
        matches!(
            self,
            CaptureEvent::AppSwitch { .. } | CaptureEvent::BrowserNavigation { .. }
        )
    }

    fn requires_visual_change_gate(&self) -> bool {
        matches!(
            self,
            CaptureEvent::AppSwitch { .. }
                | CaptureEvent::BrowserNavigation { .. }
                | CaptureEvent::MouseClick { .. }
                | CaptureEvent::KeyPause
                | CaptureEvent::Scroll
        )
    }

    fn matches_context(&self, actual: &AXInfo) -> bool {
        let Some(expected) = self.context_info() else {
            return true;
        };

        if !same_app_identity(&expected, actual) {
            return false;
        }

        match self {
            // 完整 AX 阶段偶尔拿不到 URL；此时先允许继续，并由采集后的轻量快照复核。
            // 如果本阶段明确拿到了不同 URL，则立即丢弃过期事件。
            CaptureEvent::BrowserNavigation { url, .. } => actual
                .url
                .as_deref()
                .map(str::trim)
                .map(|actual_url| actual_url == url.trim())
                .unwrap_or(true),
            _ => true,
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// CaptureEngine
// ─────────────────────────────────────────────────────────────────────────────

/// 核心采集引擎，协调所有采集步骤。
#[derive(Debug, Clone)]
struct CachedContext {
    app_name: Option<String>,
    app_bundle_id: Option<String>,
    win_title: Option<String>,
    url: Option<String>,
    webpage_title: Option<String>,
    focused_role: Option<String>,
    focused_id: Option<String>,
    observed_at: Instant,
}

impl CachedContext {
    fn has_context(&self) -> bool {
        self.app_name.is_some() || self.win_title.is_some() || self.app_bundle_id.is_some()
    }

    fn from_ax_info(info: &AXInfo) -> Self {
        Self {
            app_name: info.app_name.clone(),
            app_bundle_id: info.app_bundle_id.clone(),
            win_title: info.win_title.clone(),
            url: info.url.clone(),
            webpage_title: info.webpage_title.clone(),
            focused_role: info.focused_role.clone(),
            focused_id: info.focused_id.clone(),
            observed_at: Instant::now(),
        }
    }

    fn is_fresh(&self) -> bool {
        self.observed_at.elapsed() <= Duration::from_secs(10)
    }

    fn matches_identity(&self, info: &AXInfo) -> bool {
        match (self.app_bundle_id.as_deref(), info.app_bundle_id.as_deref()) {
            (Some(cached), Some(current)) => cached == current,
            _ => match (self.app_name.as_deref(), info.app_name.as_deref()) {
                (Some(cached), Some(current)) => cached == current,
                _ => false,
            },
        }
    }

    fn into_ax_info(self) -> AXInfo {
        AXInfo {
            app_name: self.app_name,
            app_bundle_id: self.app_bundle_id,
            win_title: self.win_title,
            url: self.url,
            webpage_title: self.webpage_title,
            focused_role: self.focused_role,
            focused_id: self.focused_id,
            ..Default::default()
        }
    }
}

#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct CaptureSceneKey {
    app_identity: String,
    page_identity: String,
}

impl CaptureSceneKey {
    fn from_ax_info(info: &AXInfo) -> Option<Self> {
        let app_identity = info
            .app_bundle_id
            .as_deref()
            .or(info.app_name.as_deref())?
            .trim();
        if app_identity.is_empty() {
            return None;
        }

        let page_identity = info
            .url
            .as_deref()
            .or(info.webpage_title.as_deref())
            .or(info.win_title.as_deref())
            .unwrap_or("")
            .trim();
        Some(Self {
            app_identity: app_identity.to_string(),
            page_identity: page_identity.to_string(),
        })
    }
}

#[derive(Debug, Clone)]
struct RecentCaptureFingerprint {
    ts_ms: i64,
    capture_id: i64,
    ax_text_hash: Option<u64>,
    dhash: Option<u64>,
    screenshot_path: Option<String>,
}

const CAPTURE_DEDUP_WINDOW_MS: i64 = 5 * 60 * 1000;
const CAPTURE_DHASH_SKIP_DISTANCE: u32 = 1;
const CAPTURE_MAX_RECENT_PER_SCENE: usize = 8;
const OCR_BACKFILL_MAX_PENDING: usize = 4;
const OCR_BACKFILL_EVENT_HISTORY_LIMIT: usize = 50_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum OcrBackfillOutcome {
    Success,
    Empty,
    Failed,
    Timeout,
    SkippedOffline,
    SkippedBackpressure,
}

impl OcrBackfillOutcome {
    fn as_str(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Empty => "empty",
            Self::Failed => "failed",
            Self::Timeout => "timeout",
            Self::SkippedOffline => "skipped_offline",
            Self::SkippedBackpressure => "skipped_backpressure",
        }
    }
}

#[derive(Debug, Clone)]
struct OcrBackfillEvent {
    ts_ms: i64,
    outcome: OcrBackfillOutcome,
    latency_ms: i64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct OcrBackfillMetricsSnapshot {
    pub submitted_total: u64,
    pub completed_total: u64,
    pub succeeded_total: u64,
    pub failed_total: u64,
    pub timed_out_total: u64,
    pub empty_total: u64,
    pub skipped_offline_total: u64,
    pub skipped_backpressure_total: u64,
    pub queued_count: usize,
    pub in_progress_count: usize,
    pub backlog_count: usize,
    pub period_completed: u64,
    pub period_succeeded: u64,
    pub period_failed: u64,
    pub period_timed_out: u64,
    pub period_empty: u64,
    pub period_skipped_offline: u64,
    pub period_skipped_backpressure: u64,
    pub period_success_rate: f64,
    pub period_throughput_per_min: f64,
    pub avg_latency_ms: i64,
    pub last_submitted_at_ms: Option<i64>,
    pub last_completed_at_ms: Option<i64>,
    pub recent: Vec<OcrBackfillRecentItem>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct OcrBackfillRecentItem {
    pub ts: i64,
    pub status: String,
    pub latency_ms: i64,
}

struct OcrBackfillMetrics {
    submitted_total: AtomicU64,
    completed_total: AtomicU64,
    succeeded_total: AtomicU64,
    failed_total: AtomicU64,
    timed_out_total: AtomicU64,
    empty_total: AtomicU64,
    skipped_offline_total: AtomicU64,
    skipped_backpressure_total: AtomicU64,
    queued_count: AtomicUsize,
    in_progress_count: AtomicUsize,
    last_submitted_at_ms: AtomicI64,
    last_completed_at_ms: AtomicI64,
    events: Mutex<VecDeque<OcrBackfillEvent>>,
}

impl OcrBackfillMetrics {
    fn new() -> Self {
        Self {
            submitted_total: AtomicU64::new(0),
            completed_total: AtomicU64::new(0),
            succeeded_total: AtomicU64::new(0),
            failed_total: AtomicU64::new(0),
            timed_out_total: AtomicU64::new(0),
            empty_total: AtomicU64::new(0),
            skipped_offline_total: AtomicU64::new(0),
            skipped_backpressure_total: AtomicU64::new(0),
            queued_count: AtomicUsize::new(0),
            in_progress_count: AtomicUsize::new(0),
            last_submitted_at_ms: AtomicI64::new(0),
            last_completed_at_ms: AtomicI64::new(0),
            events: Mutex::new(VecDeque::new()),
        }
    }

    fn record_submitted(&self, ts_ms: i64) {
        self.submitted_total.fetch_add(1, Ordering::Relaxed);
        self.queued_count.fetch_add(1, Ordering::Relaxed);
        self.last_submitted_at_ms.store(ts_ms, Ordering::Relaxed);
    }

    fn record_started(&self) {
        self.queued_count
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                Some(value.saturating_sub(1))
            })
            .ok();
        self.in_progress_count.fetch_add(1, Ordering::Relaxed);
    }

    fn record_completed(&self, outcome: OcrBackfillOutcome, ts_ms: i64, latency_ms: i64) {
        self.in_progress_count
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                Some(value.saturating_sub(1))
            })
            .ok();
        self.completed_total.fetch_add(1, Ordering::Relaxed);
        self.last_completed_at_ms.store(ts_ms, Ordering::Relaxed);

        match outcome {
            OcrBackfillOutcome::Success => {
                self.succeeded_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::Empty => {
                self.empty_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::Failed => {
                self.failed_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::Timeout => {
                self.timed_out_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::SkippedOffline => {
                self.skipped_offline_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::SkippedBackpressure => {
                self.skipped_backpressure_total
                    .fetch_add(1, Ordering::Relaxed);
            }
        }

        if let Ok(mut events) = self.events.lock() {
            events.push_back(OcrBackfillEvent {
                ts_ms,
                outcome,
                latency_ms,
            });
            while events.len() > OCR_BACKFILL_EVENT_HISTORY_LIMIT {
                events.pop_front();
            }
        }
    }

    fn record_skipped_without_queue(&self, outcome: OcrBackfillOutcome, ts_ms: i64) {
        self.submitted_total.fetch_add(1, Ordering::Relaxed);
        self.completed_total.fetch_add(1, Ordering::Relaxed);
        self.last_submitted_at_ms.store(ts_ms, Ordering::Relaxed);
        self.last_completed_at_ms.store(ts_ms, Ordering::Relaxed);
        self.record_outcome_count(outcome);
        self.push_event(OcrBackfillEvent {
            ts_ms,
            outcome,
            latency_ms: 0,
        });
    }

    fn record_aborted_before_start(&self, outcome: OcrBackfillOutcome, ts_ms: i64) {
        self.queued_count
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| {
                Some(value.saturating_sub(1))
            })
            .ok();
        self.completed_total.fetch_add(1, Ordering::Relaxed);
        self.last_completed_at_ms.store(ts_ms, Ordering::Relaxed);
        self.record_outcome_count(outcome);
        self.push_event(OcrBackfillEvent {
            ts_ms,
            outcome,
            latency_ms: 0,
        });
    }

    fn record_outcome_count(&self, outcome: OcrBackfillOutcome) {
        match outcome {
            OcrBackfillOutcome::Success => {
                self.succeeded_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::Empty => {
                self.empty_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::Failed => {
                self.failed_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::Timeout => {
                self.timed_out_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::SkippedOffline => {
                self.skipped_offline_total.fetch_add(1, Ordering::Relaxed);
            }
            OcrBackfillOutcome::SkippedBackpressure => {
                self.skipped_backpressure_total
                    .fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    fn push_event(&self, event: OcrBackfillEvent) {
        if let Ok(mut events) = self.events.lock() {
            events.push_back(event);
            while events.len() > OCR_BACKFILL_EVENT_HISTORY_LIMIT {
                events.pop_front();
            }
        }
    }

    fn snapshot(&self, range_ms: i64, now_ms: i64) -> OcrBackfillMetricsSnapshot {
        let from_ms = now_ms.saturating_sub(range_ms.max(1));
        let queued_count = self.queued_count.load(Ordering::Relaxed);
        let in_progress_count = self.in_progress_count.load(Ordering::Relaxed);
        let last_submitted = optional_ms(self.last_submitted_at_ms.load(Ordering::Relaxed));
        let last_completed = optional_ms(self.last_completed_at_ms.load(Ordering::Relaxed));

        let mut period_completed = 0u64;
        let mut period_succeeded = 0u64;
        let mut period_failed = 0u64;
        let mut period_timed_out = 0u64;
        let mut period_empty = 0u64;
        let mut period_skipped_offline = 0u64;
        let mut period_skipped_backpressure = 0u64;
        let mut latency_total = 0i64;
        let mut latency_count = 0i64;
        let mut recent = Vec::new();

        if let Ok(events) = self.events.lock() {
            for event in events.iter().filter(|event| event.ts_ms >= from_ms) {
                period_completed += 1;
                match event.outcome {
                    OcrBackfillOutcome::Success => period_succeeded += 1,
                    OcrBackfillOutcome::Empty => period_empty += 1,
                    OcrBackfillOutcome::Failed => period_failed += 1,
                    OcrBackfillOutcome::Timeout => period_timed_out += 1,
                    OcrBackfillOutcome::SkippedOffline => period_skipped_offline += 1,
                    OcrBackfillOutcome::SkippedBackpressure => period_skipped_backpressure += 1,
                }
                if event.latency_ms > 0 {
                    latency_total += event.latency_ms;
                    latency_count += 1;
                }
            }
            recent = events
                .iter()
                .rev()
                .take(8)
                .map(|event| OcrBackfillRecentItem {
                    ts: event.ts_ms,
                    status: event.outcome.as_str().to_string(),
                    latency_ms: event.latency_ms,
                })
                .collect();
        }

        let denominator = period_succeeded
            + period_failed
            + period_timed_out
            + period_empty
            + period_skipped_offline
            + period_skipped_backpressure;
        let period_success_rate = if denominator > 0 {
            period_succeeded as f64 * 100.0 / denominator as f64
        } else {
            0.0
        };
        let period_throughput_per_min = if range_ms > 0 {
            period_completed as f64 / (range_ms as f64 / 60_000.0)
        } else {
            0.0
        };
        let avg_latency_ms = if latency_count > 0 {
            latency_total / latency_count
        } else {
            0
        };

        OcrBackfillMetricsSnapshot {
            submitted_total: self.submitted_total.load(Ordering::Relaxed),
            completed_total: self.completed_total.load(Ordering::Relaxed),
            succeeded_total: self.succeeded_total.load(Ordering::Relaxed),
            failed_total: self.failed_total.load(Ordering::Relaxed),
            timed_out_total: self.timed_out_total.load(Ordering::Relaxed),
            empty_total: self.empty_total.load(Ordering::Relaxed),
            skipped_offline_total: self.skipped_offline_total.load(Ordering::Relaxed),
            skipped_backpressure_total: self.skipped_backpressure_total.load(Ordering::Relaxed),
            queued_count,
            in_progress_count,
            backlog_count: queued_count + in_progress_count,
            period_completed,
            period_succeeded,
            period_failed,
            period_timed_out,
            period_empty,
            period_skipped_offline,
            period_skipped_backpressure,
            period_success_rate,
            period_throughput_per_min,
            avg_latency_ms,
            last_submitted_at_ms: last_submitted,
            last_completed_at_ms: last_completed,
            recent,
        }
    }
}

fn ocr_metrics() -> &'static OcrBackfillMetrics {
    static METRICS: OnceLock<OcrBackfillMetrics> = OnceLock::new();
    METRICS.get_or_init(OcrBackfillMetrics::new)
}

fn optional_ms(value: i64) -> Option<i64> {
    if value > 0 {
        Some(value)
    } else {
        None
    }
}

pub fn ocr_backfill_metrics_snapshot(range_ms: i64, now_ms: i64) -> OcrBackfillMetricsSnapshot {
    ocr_metrics().snapshot(range_ms, now_ms)
}

pub struct CaptureEngine {
    storage: StorageManager,
    config: CaptureConfig,
    filter: PrivacyFilter,
    blacklist: BlacklistChecker,
    ipc_client: Option<IpcClient>,
    last_context: Mutex<Option<CachedContext>>,
    recent_captures: Mutex<HashMap<CaptureSceneKey, VecDeque<RecentCaptureFingerprint>>>,
    visual_probes: Mutex<HashMap<String, VisualFingerprint>>,
    ocr_backfill_permits: Arc<Semaphore>,
    ocr_backfill_queue_slots: Arc<Semaphore>,
}

impl CaptureEngine {
    /// 使用默认隐私过滤器创建引擎。
    pub fn new(storage: StorageManager, config: CaptureConfig) -> Self {
        let ipc_client = IpcClient::new();
        if ipc_client.is_available() {
            info!("检测到 AI Sidecar socket，OCR 将在运行时按需连接");
        } else {
            warn!("启动时未检测到 AI Sidecar socket，稍后可自动恢复 OCR 连接");
        }

        let blacklist = BlacklistChecker::new(storage.clone());

        Self {
            storage,
            config,
            filter: PrivacyFilter::new(),
            blacklist,
            ipc_client: Some(ipc_client),
            last_context: Mutex::new(None),
            recent_captures: Mutex::new(HashMap::new()),
            visual_probes: Mutex::new(HashMap::new()),
            ocr_backfill_permits: Arc::new(Semaphore::new(1)),
            ocr_backfill_queue_slots: Arc::new(Semaphore::new(OCR_BACKFILL_MAX_PENDING)),
        }
    }

    /// 使用自定义隐私过滤器创建引擎（从数据库 app_filters 加载后使用）。
    pub fn with_filter(
        storage: StorageManager,
        config: CaptureConfig,
        filter: PrivacyFilter,
    ) -> Self {
        let ipc_client = IpcClient::new();
        if ipc_client.is_available() {
            info!("检测到 AI Sidecar socket，OCR 将在运行时按需连接");
        } else {
            warn!("启动时未检测到 AI Sidecar socket，稍后可自动恢复 OCR 连接");
        }

        let blacklist = BlacklistChecker::new(storage.clone());

        Self {
            storage,
            config,
            filter,
            blacklist,
            ipc_client: Some(ipc_client),
            last_context: Mutex::new(None),
            recent_captures: Mutex::new(HashMap::new()),
            visual_probes: Mutex::new(HashMap::new()),
            ocr_backfill_permits: Arc::new(Semaphore::new(1)),
            ocr_backfill_queue_slots: Arc::new(Semaphore::new(OCR_BACKFILL_MAX_PENDING)),
        }
    }

    // ── 主处理流程 ─────────────────────────────────────────────────────────

    /// 处理一个采集事件：AX 抓取 + 必要时截图/OCR + 隐私过滤 + DB 写入。
    ///
    /// 返回 `Ok(Some(id))`：已写入数据库的 capture id；隐私拦截或重复内容返回 `Ok(None)`。
    pub async fn process_event(&self, event: CaptureEvent) -> Result<Option<i64>, CaptureError> {
        let requires_visual_gate = event.requires_visual_change_gate();
        // 明确信号自带上下文；键鼠信号先只读取应用/窗口元数据。隐私门禁必须先于
        // 像素探针，避免对密码管理器或敏感窗口调用系统截图 API。
        let mut gate_context = event.context_info();
        if requires_visual_gate && gate_context.is_none() {
            gate_context = get_frontmost_context_snapshot_async().await;
        }
        if let Some(context) = gate_context.as_ref() {
            if is_system_session_context(
                context.app_bundle_id.as_deref(),
                context.app_name.as_deref(),
            ) {
                self.reset_cached_context();
                debug!(
                    event = ?event.to_event_type(),
                    app = ?context.app_name,
                    bundle_id = ?context.app_bundle_id,
                    "登录/锁屏系统会话不属于工作内容，跳过采集"
                );
                self.record_attempt(
                    &event,
                    "privacy_skipped",
                    "system_session",
                    Some(&context),
                    None,
                    None,
                    true,
                );
                return Ok(None);
            }
            let blacklisted = self.blacklist.is_blacklisted_by_bundle_or_name(
                context.app_bundle_id.as_deref(),
                context.app_name.as_deref(),
            );
            let sensitive = self.filter.is_sensitive(
                context.app_name.as_deref(),
                context.app_bundle_id.as_deref(),
                context.focused_role.as_deref(),
                context.win_title.as_deref(),
            );
            if blacklisted || sensitive {
                if blacklisted {
                    self.record_blacklist_block(&context);
                }
                info!(
                    event = ?event.to_event_type(),
                    app = ?context.app_name,
                    bundle_id = ?context.app_bundle_id,
                    "变化事件命中隐私门禁，跳过 AX 正文读取"
                );
                self.record_attempt(
                    &event,
                    "privacy_skipped",
                    if blacklisted {
                        "app_blacklist"
                    } else {
                        "sensitive_context"
                    },
                    Some(&context),
                    None,
                    None,
                    true,
                );
                return Ok(None);
            }
        }

        let mut visual_probe = None;
        if requires_visual_gate && self.config.enable_screenshot {
            let Some(context) = gate_context.as_ref() else {
                self.record_attempt(
                    &event,
                    "degraded",
                    "visual_context_unavailable",
                    None,
                    None,
                    None,
                    false,
                );
                return Ok(None);
            };
            match capture_visual_probe_async().await {
                Ok(Some(probe)) => {
                    if let Some((significant, difference)) =
                        self.evaluate_visual_probe(context, &probe.fingerprint)
                    {
                        if !significant {
                            debug!(
                                event = ?event.to_event_type(),
                                total_bits = difference.total_bits,
                                max_tile_bits = difference.max_tile_bits,
                                changed_tiles = difference.changed_tiles,
                                "视觉指纹变化低于阈值，放弃完整采集"
                            );
                            self.record_attempt(
                                &event,
                                "deduplicated",
                                "visual_fingerprint_below_threshold",
                                Some(context),
                                None,
                                None,
                                false,
                            );
                            return Ok(None);
                        }
                    }
                    visual_probe = Some(probe);
                }
                Ok(None) => {
                    self.record_attempt(
                        &event,
                        "degraded",
                        "visual_probe_unavailable_fallback",
                        Some(context),
                        None,
                        None,
                        false,
                    );
                }
                Err(error) => {
                    warn!(%error, event = ?event.to_event_type(), "视觉探针失败，回退到原采集链路");
                    self.record_attempt(
                        &event,
                        "degraded",
                        "visual_probe_failed_fallback",
                        Some(context),
                        None,
                        None,
                        false,
                    );
                }
            }
        }

        let ax_info = if self.config.enable_ax {
            get_frontmost_info_async().await
        } else {
            None
        };
        self.process_event_with_ax_info_and_probe(event, ax_info, true, visual_probe)
            .await
    }

    async fn process_event_with_ax_info(
        &self,
        event: CaptureEvent,
        ax_info: Option<AXInfo>,
        revalidate_context: bool,
    ) -> Result<Option<i64>, CaptureError> {
        self.process_event_with_ax_info_and_probe(event, ax_info, revalidate_context, None)
            .await
    }

    async fn process_event_with_ax_info_and_probe(
        &self,
        event: CaptureEvent,
        ax_info: Option<AXInfo>,
        revalidate_context: bool,
        mut visual_probe: Option<VisualProbe>,
    ) -> Result<Option<i64>, CaptureError> {
        let ts = current_ts_ms();
        let is_context_change_event = event.is_context_change_event();
        if is_context_change_event
            && ax_info
                .as_ref()
                .is_some_and(|actual| !event.matches_context(actual))
        {
            debug!(
                event = ?event.to_event_type(),
                expected = ?event.context_info(),
                actual = ?ax_info,
                "变化事件处理时前台上下文已改变，丢弃过期事件"
            );
            self.record_attempt(
                &event,
                "stale",
                "context_changed_before_processing",
                ax_info.as_ref(),
                None,
                None,
                false,
            );
            return Ok(None);
        }
        let ax_missing = ax_info.is_none();
        let has_fresh_context = ax_info.is_some();

        // 变化事件的应用/URL 元数据优先，但保留本轮完整 AX 抓取到的正文与焦点信息。
        let mut merged = self.merge_ax_and_event(&event, ax_info);
        if is_system_session_context(merged.app_bundle_id.as_deref(), merged.app_name.as_deref()) {
            self.reset_cached_context();
            debug!(
                event = ?event.to_event_type(),
                app = ?merged.app_name,
                bundle_id = ?merged.app_bundle_id,
                "登录/锁屏系统会话不属于工作内容，跳过采集"
            );
            self.record_attempt(
                &event,
                "privacy_skipped",
                "system_session",
                Some(&merged),
                None,
                None,
                true,
            );
            return Ok(None);
        }
        let cache_hit = ax_missing
            && (merged.app_name.is_some()
                || merged.win_title.is_some()
                || merged.app_bundle_id.is_some());
        let is_periodic_event = matches!(event, CaptureEvent::Periodic);
        let has_ax_text = has_meaningful_ax_text(&merged);
        let has_input_text = has_meaningful_input_text(&event);

        debug!(
            event = ?event.to_event_type(),
            ax_missing,
            cache_hit,
            app = ?merged.app_name,
            win_title = ?merged.win_title,
            ax_text_len = merged.extracted_text.as_ref().map(|t| t.len()),
            "AX 与上下文合并完成"
        );

        // 即使随后命中隐私/应用黑名单，也要刷新内存中的当前应用身份。
        // 否则下一次 AX 失败时可能回退到上一应用，造成跨应用上下文错配。
        if has_fresh_context {
            self.update_cached_context(&merged);
        }

        // 3. 应用黑名单检测（优先级最高，快速跳过）
        if self.blacklist.is_blacklisted_by_bundle_or_name(
            merged.app_bundle_id.as_deref(),
            merged.app_name.as_deref(),
        ) {
            info!(
                app = ?merged.app_name,
                bundle_id = ?merged.app_bundle_id,
                "应用在黑名单中，跳过采集"
            );
            self.record_blacklist_block(&merged);
            self.record_attempt(
                &event,
                "privacy_skipped",
                "app_blacklist",
                Some(&merged),
                None,
                None,
                true,
            );
            return Ok(None);
        }

        // 4. 隐私过滤
        let is_sensitive = self.filter.is_sensitive(
            merged.app_name.as_deref(),
            merged.app_bundle_id.as_deref(),
            merged.focused_role.as_deref(),
            merged.win_title.as_deref(),
        );

        if is_sensitive {
            error!(
                event = ?event.to_event_type(),
                app = ?merged.app_name,
                bundle_id = ?merged.app_bundle_id,
                focused_role = ?merged.focused_role,
                win_title = ?merged.win_title,
                has_input_text,
                has_ax_text,
                reason = "sensitive_capture_with_empty_text",
                "capture dropped: empty_text_payload"
            );
            self.record_attempt(
                &event,
                "privacy_skipped",
                "sensitive_context",
                Some(&merged),
                None,
                None,
                true,
            );
            return Ok(None);
        }

        if revalidate_context
            && is_context_change_event
            && !self.context_still_matches(&event).await
        {
            debug!(
                event = ?event.to_event_type(),
                app = ?merged.app_name,
                url = ?merged.url,
                "正文抓取后前台上下文已改变，丢弃过期变化采集"
            );
            self.record_attempt(
                &event,
                "stale",
                "context_changed_after_ax",
                Some(&merged),
                None,
                None,
                false,
            );
            return Ok(None);
        }

        let ax_text_hash = meaningful_ax_text_hash(&merged);
        let mut continuity_reason: Option<&str> = None;
        let mut related_capture_id = None;
        if is_periodic_event && ax_text_hash.is_some() {
            if let Some(previous) = self.find_recent_duplicate(&merged, ts, ax_text_hash, None) {
                continuity_reason = Some("periodic_ax_text_duplicate");
                related_capture_id = Some(previous.capture_id);
                debug!(
                    event = ?event.to_event_type(),
                    app = ?merged.app_name,
                    url = ?merged.url,
                    related_capture_id = previous.capture_id,
                    "定时 AX 内容重复，保留轻量连续性记录"
                );
            }
        }

        // AX 正文优先：只有 AX 正文为空时才截图，并把截图交给后台 OCR。
        let mut screenshot_path = None;
        let mut screenshot_source: Option<String> = None;
        let mut screenshot_dhash = None;
        let mut duplicate_capture_id = None;
        if !has_ax_text && self.config.enable_screenshot {
            let screenshot_result = match visual_probe.take() {
                Some(probe) => save_visual_probe_async(
                    probe,
                    self.config.captures_dir.clone(),
                    self.config.screenshot_quality,
                )
                .await
                .map(Some),
                None => {
                    capture_and_save_async(
                        self.config.captures_dir.clone(),
                        self.config.screenshot_quality,
                    )
                    .await
                }
            };
            match screenshot_result {
                Ok(Some(result)) => {
                    if merged.app_name.as_deref().unwrap_or("").trim().is_empty() {
                        merged.app_name = result.app_name.clone();
                    }
                    if merged.win_title.as_deref().unwrap_or("").trim().is_empty() {
                        merged.win_title = result.window_title.clone();
                    }
                    if self.filter.is_sensitive(
                        merged.app_name.as_deref(),
                        merged.app_bundle_id.as_deref(),
                        merged.focused_role.as_deref(),
                        merged.win_title.as_deref(),
                    ) {
                        delete_screenshot_file(&result.full_path);
                        error!(
                            event = ?event.to_event_type(),
                            app = ?merged.app_name,
                            bundle_id = ?merged.app_bundle_id,
                            focused_role = ?merged.focused_role,
                            win_title = ?merged.win_title,
                            reason = "sensitive_capture_after_screenshot_context",
                            "capture dropped: sensitive metadata recovered from screenshot context"
                        );
                        self.record_attempt(
                            &event,
                            "privacy_skipped",
                            "sensitive_context_after_screenshot",
                            Some(&merged),
                            None,
                            None,
                            true,
                        );
                        return Ok(None);
                    }

                    if revalidate_context
                        && is_context_change_event
                        && !self.context_still_matches(&event).await
                    {
                        delete_screenshot_file(&result.full_path);
                        debug!(
                            event = ?event.to_event_type(),
                            path = %result.relative_path,
                            "截图后前台上下文已改变，删除错配截图"
                        );
                        self.record_attempt(
                            &event,
                            "stale",
                            "context_changed_after_screenshot",
                            Some(&merged),
                            None,
                            None,
                            false,
                        );
                        return Ok(None);
                    }

                    let duplicate =
                        self.find_recent_duplicate(&merged, ts, None, Some(result.dhash));
                    if let Some(previous) = duplicate.as_ref() {
                        if is_periodic_event {
                            continuity_reason = Some("periodic_visual_duplicate");
                            related_capture_id = Some(previous.capture_id);
                        }
                        if let Some(previous_path) = previous.screenshot_path.as_deref() {
                            let previous_full_path = self.config.captures_dir.join(previous_path);
                            if replace_with_hard_link(&previous_full_path, &result.full_path) {
                                duplicate_capture_id = Some(previous.capture_id);
                                debug!(
                                    capture_id = previous.capture_id,
                                    path = %result.relative_path,
                                    "变化采集视觉重复，复用已有截图数据块"
                                );
                            }
                        }
                    }

                    debug!(
                        path = %result.relative_path,
                        dhash = result.dhash,
                        source = %result.source.as_str(),
                        "截图关键帧已保存"
                    );
                    screenshot_dhash = Some(result.dhash);
                    screenshot_path = Some(result.relative_path);
                    screenshot_source = Some(result.source.as_str().to_string());
                }
                Ok(None) => {
                    debug!(event = ?event.to_event_type(), "跳过截图：screenshot_unavailable");
                }
                Err(error) => {
                    if !has_ax_text && !has_input_text {
                        self.record_attempt(
                            &event,
                            "failed",
                            "screenshot_failed_without_text",
                            Some(&merged),
                            None,
                            None,
                            false,
                        );
                        return Err(error);
                    }
                    warn!(
                        event = ?event.to_event_type(),
                        app = ?merged.app_name,
                        win_title = ?merged.win_title,
                        %error,
                        "截图失败，继续保存已有文本采集"
                    );
                }
            }
        } else {
            debug!(
                event = ?event.to_event_type(),
                has_ax_text,
                screenshot_enabled = self.config.enable_screenshot,
                "跳过截图：AX 正文已满足或截图已关闭"
            );
        }

        let ocr_text_for_insert = duplicate_capture_id
            .and_then(|capture_id| self.storage.get_capture(capture_id).ok().flatten())
            .and_then(|capture| capture.ocr_text)
            .filter(|text| !text.trim().is_empty());
        if !has_ax_text
            && !has_input_text
            && ocr_text_for_insert.is_none()
            && screenshot_path.is_none()
        {
            error!(
                event = ?event.to_event_type(),
                app = ?merged.app_name,
                win_title = ?merged.win_title,
                has_input_text,
                has_ax_text,
                screenshot_present = false,
                reason = "empty_text_payload",
                "capture dropped: empty_text_payload"
            );
            self.record_attempt(
                &event,
                "failed",
                "empty_text_payload",
                Some(&merged),
                None,
                None,
                false,
            );
            return Ok(None);
        }

        // 5. 写入数据库
        let id = match self.save_capture(
            ts,
            &merged,
            &event,
            screenshot_path.clone(),
            screenshot_source.clone(),
            ocr_text_for_insert.clone(),
            false,
        ) {
            Ok(id) => id,
            Err(error) => {
                self.record_attempt(
                    &event,
                    "failed",
                    "storage_insert_failed",
                    Some(&merged),
                    None,
                    related_capture_id,
                    false,
                );
                return Err(error);
            }
        };
        self.update_cached_context(&merged);
        let mut ocr_backfill_enqueued = false;
        if !has_ax_text {
            if ocr_text_for_insert.is_none() {
                if let Some(screenshot_rel_path) = screenshot_path.clone() {
                    ocr_backfill_enqueued = self.enqueue_ocr_backfill(
                        id,
                        screenshot_rel_path,
                        merged.app_name.clone(),
                        merged.win_title.clone(),
                    );
                }
            }
        }
        if ax_text_hash.is_some() || screenshot_dhash.is_some() {
            self.record_recent_capture(
                &merged,
                RecentCaptureFingerprint {
                    ts_ms: ts,
                    capture_id: id,
                    ax_text_hash,
                    dhash: screenshot_dhash,
                    screenshot_path: screenshot_path.clone(),
                },
            );
        }
        debug!(
            id,
            event = ?event.to_event_type(),
            app = ?merged.app_name,
            win_title = ?merged.win_title,
            ax_text_len = merged.extracted_text.as_ref().map(|t| t.len()),
            ocr_text_len = ocr_text_for_insert.as_ref().map(|t| t.len()),
            screenshot = screenshot_path.is_some(),
            ocr_backfill_enqueued,
            "采集完成"
        );
        self.record_attempt(
            &event,
            if continuity_reason.is_some() {
                "continuity"
            } else {
                "captured"
            },
            continuity_reason.unwrap_or("capture_persisted"),
            Some(&merged),
            Some(id),
            related_capture_id,
            false,
        );

        Ok(Some(id))
    }

    /// 启动事件处理循环（生产环境入口）。
    ///
    /// 从 channel 持续接收事件直到发送端关闭。
    pub async fn run(self, mut rx: mpsc::Receiver<CaptureEvent>) -> Result<(), CaptureError> {
        info!("CaptureEngine 已启动");

        while let Some(event) = rx.recv().await {
            match self.process_event(event).await {
                Ok(Some(id)) => debug!(id, "事件处理完成"),
                Ok(None) => {}
                Err(e) => warn!("事件处理失败: {}", e),
            }
        }

        info!("CaptureEngine 退出（channel 已关闭）");
        Ok(())
    }

    // ── 辅助方法 ──────────────────────────────────────────────────────────

    /// 合并事件内置信息、完整 AX 抓取结果与最近一次成功上下文。
    ///
    /// 事件显式的应用/页面身份优先；完整 AX 抓取到的正文和焦点信息必须保留。
    /// 缓存只在应用身份一致时补空字段，避免跨应用上下文错配。
    fn merge_ax_and_event(&self, event: &CaptureEvent, ax: Option<AXInfo>) -> AXInfo {
        let cached = self
            .last_context
            .lock()
            .ok()
            .and_then(|guard| guard.clone())
            .filter(CachedContext::is_fresh);

        let current_ax = ax;
        let event_context = event.context_info();
        let Some(mut info) = current_ax.or_else(|| event_context.clone()) else {
            return cached.map(CachedContext::into_ax_info).unwrap_or_default();
        };

        if let Some(event_context) = event_context {
            info.app_name = event_context.app_name.or(info.app_name);
            info.app_bundle_id = event_context.app_bundle_id.or(info.app_bundle_id);
            info.win_title = event_context.win_title.or(info.win_title);
            info.url = event_context.url.or(info.url);
            info.webpage_title = event_context.webpage_title.or(info.webpage_title);
        }

        if let Some(cached) = cached.filter(|cached| cached.matches_identity(&info)) {
            if info.app_name.is_none() {
                info.app_name = cached.app_name;
            }
            if info.app_bundle_id.is_none() {
                info.app_bundle_id = cached.app_bundle_id;
            }
            if info.win_title.is_none() {
                info.win_title = cached.win_title;
            }
            if info.url.is_none() {
                info.url = cached.url;
            }
            if info.webpage_title.is_none() {
                info.webpage_title = cached.webpage_title;
            }
            if info.focused_role.is_none() {
                info.focused_role = cached.focused_role;
            }
            if info.focused_id.is_none() {
                info.focused_id = cached.focused_id;
            }
        }

        info
    }

    async fn context_still_matches(&self, event: &CaptureEvent) -> bool {
        if !event.is_context_change_event() {
            return true;
        }

        match get_frontmost_context_snapshot_async().await {
            Some(actual) => event.matches_context(&actual),
            None => {
                debug!(
                    event = ?event.to_event_type(),
                    "前台上下文复核暂不可用，沿用完整 AX 阶段的一致性结果"
                );
                true
            }
        }
    }

    fn update_cached_context(&self, info: &AXInfo) {
        let context = CachedContext::from_ax_info(info);
        if !context.has_context() {
            return;
        }

        if let Ok(mut guard) = self.last_context.lock() {
            *guard = Some(context);
        }
    }

    fn reset_cached_context(&self) {
        if let Ok(mut guard) = self.last_context.lock() {
            *guard = None;
        }
    }

    fn record_blacklist_block(&self, info: &AXInfo) {
        let storage = self.storage.clone();
        let stat_target = info.app_bundle_id.clone().or_else(|| info.app_name.clone());
        if let Some(target_id) = stat_target {
            tokio::spawn(async move {
                let _ = storage.with_conn(|conn| {
                    crate::storage::repo::privacy::increment_block_stat(
                        conn,
                        "blacklist",
                        &target_id,
                    )
                });
            });
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn record_attempt(
        &self,
        event: &CaptureEvent,
        outcome: &str,
        reason: &str,
        info: Option<&AXInfo>,
        capture_id: Option<i64>,
        related_capture_id: Option<i64>,
        is_private: bool,
    ) {
        let attempt = NewCaptureAttempt {
            observed_at: current_ts_ms(),
            event_type: event.to_event_type().as_str().to_string(),
            outcome: outcome.to_string(),
            reason: reason.to_string(),
            capture_id,
            related_capture_id,
            app_name: if is_private {
                None
            } else {
                info.and_then(|value| value.app_name.clone())
            },
            win_title: if is_private {
                None
            } else {
                info.and_then(|value| value.win_title.clone())
            },
            is_private,
            effective_interval_secs: None,
        };
        if let Err(error) = self.storage.insert_capture_attempt(&attempt) {
            warn!(%error, outcome, reason, "记录采集尝试审计失败");
        }
    }

    /// 返回与同一前台应用上一探针的差异，并无论结果如何更新基线。按应用而不是 URL
    /// 分组，保证 URL 变化也会真正比较画面，而不是因为新 URL 没有基线而直接放行。
    fn evaluate_visual_probe(
        &self,
        info: &AXInfo,
        current: &VisualFingerprint,
    ) -> Option<(bool, VisualDifference)> {
        let app_identity = info
            .app_bundle_id
            .as_deref()
            .or(info.app_name.as_deref())?
            .trim();
        if app_identity.is_empty() {
            return None;
        }
        let Ok(mut probes) = self.visual_probes.lock() else {
            return None;
        };
        let previous = probes.insert(app_identity.to_string(), current.clone());
        previous.map(|previous| {
            let difference = previous.difference(current);
            (previous.is_significant_change(current), difference)
        })
    }

    fn find_recent_duplicate(
        &self,
        info: &AXInfo,
        ts: i64,
        ax_text_hash: Option<u64>,
        dhash: Option<u64>,
    ) -> Option<RecentCaptureFingerprint> {
        let scene_key = CaptureSceneKey::from_ax_info(info)?;
        let Ok(mut guard) = self.recent_captures.lock() else {
            return None;
        };

        let entry = guard.entry(scene_key).or_default();
        prune_recent_captures(entry, ts);
        entry
            .iter()
            .rev()
            .find(|capture| {
                let same_ax = ax_text_hash.is_some()
                    && capture.ax_text_hash.is_some()
                    && ax_text_hash == capture.ax_text_hash;
                let same_visual = match (dhash, capture.dhash) {
                    (Some(current), Some(previous)) => {
                        hamming_distance(current, previous) <= CAPTURE_DHASH_SKIP_DISTANCE
                    }
                    _ => false,
                };
                same_ax || same_visual
            })
            .cloned()
    }

    fn record_recent_capture(&self, info: &AXInfo, capture: RecentCaptureFingerprint) {
        let Some(scene_key) = CaptureSceneKey::from_ax_info(info) else {
            return;
        };
        let Ok(mut guard) = self.recent_captures.lock() else {
            return;
        };

        let entry = guard.entry(scene_key).or_default();
        prune_recent_captures(entry, capture.ts_ms);
        entry.push_back(capture);
        while entry.len() > CAPTURE_MAX_RECENT_PER_SCENE {
            entry.pop_front();
        }
    }

    /// 构造并写入 captures 记录。
    fn save_capture(
        &self,
        ts: i64,
        ax: &AXInfo,
        event: &CaptureEvent,
        screenshot_path: Option<String>,
        screenshot_source: Option<String>,
        ocr_text: Option<String>,
        is_sensitive: bool,
    ) -> Result<i64, CaptureError> {
        let content_filter = if is_sensitive {
            None
        } else {
            Some(ContentFilter::from_storage(&self.storage))
        };
        let ax_text = self.filter_optional_text(&content_filter, ax.extracted_text.clone())?;
        let input_text =
            self.filter_optional_text(&content_filter, event.input_text().map(str::to_string))?;
        let ocr_text = self.filter_optional_text(&content_filter, ocr_text)?;
        let pii_scrubbed = ax_text.1 || input_text.1 || ocr_text.1;

        let new_capture = NewCapture {
            ts,
            app_name: ax.app_name.clone(),
            app_bundle_id: ax.app_bundle_id.clone(),
            // 敏感记录不保存窗口标题
            win_title: if is_sensitive {
                None
            } else {
                ax.win_title.clone()
            },
            event_type: event.to_event_type(),
            // 敏感记录不保存文本
            ax_text: if is_sensitive { None } else { ax_text.0 },
            ax_focused_role: if is_sensitive {
                None
            } else {
                ax.focused_role.clone()
            },
            ax_focused_id: if is_sensitive {
                None
            } else {
                ax.focused_id.clone()
            },
            ocr_text: if is_sensitive { None } else { ocr_text.0 },
            screenshot_path,
            screenshot_source,
            input_text: if is_sensitive { None } else { input_text.0 },
            is_sensitive,
            pii_scrubbed,
            url: if is_sensitive { None } else { ax.url.clone() },
            webpage_title: if is_sensitive {
                None
            } else {
                ax.webpage_title.clone()
            },
        };
        Ok(self.storage.insert_capture(&new_capture)?)
    }

    fn enqueue_ocr_backfill(
        &self,
        capture_id: i64,
        screenshot_rel_path: String,
        app_name: Option<String>,
        win_title: Option<String>,
    ) -> bool {
        let submitted_at_ms = current_ts_ms();
        let Some(ipc_client) = self.ipc_client.clone() else {
            debug!(capture_id, "跳过 OCR 后台补写：sidecar_unavailable");
            ocr_metrics()
                .record_skipped_without_queue(OcrBackfillOutcome::SkippedOffline, submitted_at_ms);
            return false;
        };

        let Ok(queue_slot) = self.ocr_backfill_queue_slots.clone().try_acquire_owned() else {
            warn!(
                capture_id,
                max_pending = OCR_BACKFILL_MAX_PENDING,
                "跳过 OCR 后台补写：队列已满，保留截图等待后续兜底"
            );
            ocr_metrics().record_skipped_without_queue(
                OcrBackfillOutcome::SkippedBackpressure,
                submitted_at_ms,
            );
            return false;
        };

        ocr_metrics().record_submitted(submitted_at_ms);
        let storage = self.storage.clone();
        let captures_dir = self.config.captures_dir.clone();
        let permits = self.ocr_backfill_permits.clone();

        tokio::spawn(async move {
            let _queue_slot = queue_slot;
            let Ok(_permit) = permits.acquire_owned().await else {
                warn!(capture_id, "OCR 后台补写获取限流许可失败");
                ocr_metrics()
                    .record_aborted_before_start(OcrBackfillOutcome::Failed, current_ts_ms());
                return;
            };
            ocr_metrics().record_started();

            if !ipc_client.ping().await {
                debug!(capture_id, app = ?app_name, "跳过 OCR 后台补写：sidecar_offline");
                ocr_metrics().record_completed(
                    OcrBackfillOutcome::SkippedOffline,
                    current_ts_ms(),
                    0,
                );
                return;
            }

            let screenshot_path = captures_dir.join(&screenshot_rel_path);
            let path_for_ocr = screenshot_path.to_string_lossy().to_string();
            let started_at_ms = current_ts_ms();
            debug!(
                capture_id,
                path = %screenshot_rel_path,
                app = ?app_name,
                win_title = ?win_title,
                "OCR 后台补写开始"
            );

            match run_ocr_backfill(ipc_client, path_for_ocr).await {
                Ok(Some((text, confidence))) => {
                    let completed_at_ms = current_ts_ms();
                    let (filtered_text, pii_scrubbed) = filter_ocr_text_for_update(&storage, text);
                    if let Err(error) =
                        storage.update_ocr_text(capture_id, &filtered_text, confidence)
                    {
                        warn!(capture_id, %error, "OCR 后台补写失败：数据库更新失败");
                        ocr_metrics().record_completed(
                            OcrBackfillOutcome::Failed,
                            completed_at_ms,
                            completed_at_ms.saturating_sub(started_at_ms),
                        );
                        return;
                    }
                    if pii_scrubbed {
                        let _ = storage.mark_pii_scrubbed(capture_id);
                    }
                    ocr_metrics().record_completed(
                        OcrBackfillOutcome::Success,
                        completed_at_ms,
                        completed_at_ms.saturating_sub(started_at_ms),
                    );
                    debug!(
                        capture_id,
                        text_len = filtered_text.chars().count(),
                        pii_scrubbed,
                        "OCR 后台补写完成"
                    );
                }
                Ok(None) => {
                    debug!(capture_id, "OCR 后台补写返回空文本");
                    let completed_at_ms = current_ts_ms();
                    ocr_metrics().record_completed(
                        OcrBackfillOutcome::Empty,
                        completed_at_ms,
                        completed_at_ms.saturating_sub(started_at_ms),
                    );
                }
                Err(error) => {
                    warn!(capture_id, %error, "OCR 后台补写失败");
                    let completed_at_ms = current_ts_ms();
                    ocr_metrics().record_completed(
                        classify_ocr_backfill_error(&error),
                        completed_at_ms,
                        completed_at_ms.saturating_sub(started_at_ms),
                    );
                }
            }
        });
        true
    }

    fn filter_optional_text(
        &self,
        content_filter: &Option<ContentFilter>,
        text: Option<String>,
    ) -> Result<(Option<String>, bool), CaptureError> {
        let Some(text) = text else {
            return Ok((None, false));
        };
        let Some(content_filter) = content_filter else {
            return Ok((Some(text), false));
        };

        let result = content_filter.filter_text(&text);
        if result.redacted_count == 0 {
            return Ok((Some(text), false));
        }

        for filter_type in &result.hit_types {
            self.storage.with_conn(|conn| {
                crate::storage::repo::privacy::increment_block_stat(conn, "filter", filter_type)
            })?;
        }

        Ok((Some(result.text), true))
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 工具函数
// ─────────────────────────────────────────────────────────────────────────────

fn current_ts_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time went backwards")
        .as_millis() as i64
}

fn has_meaningful_ax_text(info: &AXInfo) -> bool {
    info.extracted_text
        .as_deref()
        .map(|text| !text.trim().is_empty())
        .unwrap_or(false)
}

fn has_meaningful_input_text(event: &CaptureEvent) -> bool {
    event
        .input_text()
        .map(|text| !text.trim().is_empty())
        .unwrap_or(false)
}

fn meaningful_ax_text_hash(info: &AXInfo) -> Option<u64> {
    let text = info.extracted_text.as_deref()?.trim();
    if text.is_empty() {
        return None;
    }
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    Some(hasher.finish())
}

fn same_app_identity(expected: &AXInfo, actual: &AXInfo) -> bool {
    match (
        expected.app_bundle_id.as_deref(),
        actual.app_bundle_id.as_deref(),
    ) {
        (Some(expected), Some(actual)) => expected == actual,
        _ => match (expected.app_name.as_deref(), actual.app_name.as_deref()) {
            (Some(expected), Some(actual)) => expected == actual,
            _ => false,
        },
    }
}

fn prune_recent_captures(captures: &mut VecDeque<RecentCaptureFingerprint>, now_ts: i64) {
    while let Some(front) = captures.front() {
        if now_ts - front.ts_ms > CAPTURE_DEDUP_WINDOW_MS {
            captures.pop_front();
        } else {
            break;
        }
    }
}

fn replace_with_hard_link(existing: &std::path::Path, target: &std::path::Path) -> bool {
    if !existing.is_file() || !target.is_file() {
        return false;
    }
    let temp_link = target.with_extension("dedup-link");
    if std::fs::hard_link(existing, &temp_link).is_err() {
        return false;
    }
    if std::fs::remove_file(target).is_err() {
        let _ = std::fs::remove_file(&temp_link);
        return false;
    }
    if std::fs::rename(&temp_link, target).is_ok() {
        true
    } else {
        let restored = std::fs::hard_link(existing, target).is_ok();
        let _ = std::fs::remove_file(&temp_link);
        restored
    }
}

fn delete_screenshot_file(path: &std::path::Path) {
    if let Err(err) = std::fs::remove_file(path) {
        warn!(path = ?path, "删除去重截图失败: {}", err);
    }
}

async fn run_ocr_backfill(
    ipc_client: IpcClient,
    path_for_ocr: String,
) -> Result<Option<(String, f32)>, String> {
    // spawn_blocking 中的同步 UnixStream/Vision 请求无法被 Tokio timeout 真正取消。
    // 旧实现 15 秒后释放队列许可，但阻塞线程仍继续最多重试 3 次，导致相同截图
    // 在 Sidecar 内并发执行。这里只发一次请求，并一直持有单并发许可，直到该阻塞
    // 调用真实结束；传输层自身仍有有限的读写截止时间用于处理 Sidecar 故障。
    match tokio::task::spawn_blocking(move || ipc_client.call_ocr(0, &path_for_ocr)).await {
        Ok(Ok(result)) => {
            let trimmed = result.text.trim().to_string();
            if trimmed.is_empty() {
                Ok(None)
            } else {
                Ok(Some((trimmed, result.confidence as f32)))
            }
        }
        Ok(Err(error)) => Err(error.to_string()),
        Err(error) => Err(format!("OCR 后台任务崩溃: {error}")),
    }
}

fn filter_ocr_text_for_update(storage: &StorageManager, text: String) -> (String, bool) {
    let content_filter = ContentFilter::from_storage(storage);
    let result = content_filter.filter_text(&text);
    if result.redacted_count == 0 {
        return (text, false);
    }

    for filter_type in &result.hit_types {
        let _ = storage.with_conn(|conn| {
            crate::storage::repo::privacy::increment_block_stat(conn, "filter", filter_type)
        });
    }

    (result.text, true)
}

fn classify_ocr_backfill_error(error: &str) -> OcrBackfillOutcome {
    let lower = error.to_ascii_lowercase();
    if lower.contains("timeout") || lower.contains("timed out") || error.contains("超时") {
        OcrBackfillOutcome::Timeout
    } else {
        OcrBackfillOutcome::Failed
    }
}

impl CaptureEngine {
    /// 触发文本向量化并写入向量库
    ///
    /// 流程：
    /// 1. 调用 AI Sidecar 的 Embedding API
    /// 2. 将向量元数据写入 SQLite vector_index 表
    /// 3. 实际向量数据由 Sidecar 写入 Qdrant
    async fn trigger_embedding(
        ipc_client: IpcClient,
        storage: StorageManager,
        capture_id: i64,
        text: String,
    ) {
        // 文本太短不值得向量化
        if text.trim().len() < 10 {
            debug!(capture_id, "文本过短，跳过向量化");
            return;
        }

        // 调用 Embedding API
        match ipc_client.call_embed(capture_id, vec![text.clone()]) {
            Ok(embed_result) => {
                debug!(
                    capture_id,
                    vector_count = embed_result.vectors.len(),
                    "Embedding 成功"
                );

                // 为每个向量生成唯一的 point_id（Qdrant 使用 UUID）
                let point_id = uuid::Uuid::new_v4().to_string();

                // 写入 vector_index 元数据
                let created_at = current_ts_ms();
                let index = NewVectorIndex {
                    capture_id,
                    qdrant_point_id: point_id,
                    chunk_index: 0, // 单文本不分块
                    chunk_text: text,
                    model_name: "bge-m3".to_string(), // 与 AI Sidecar 保持一致
                    created_at,
                    doc_key: format!("capture:{}", capture_id),
                    source_type: "capture".to_string(),
                    knowledge_id: None,
                    time: Some(created_at),
                    start_time: None,
                    end_time: None,
                    observed_at: None,
                    event_time_start: None,
                    event_time_end: None,
                    history_view: false,
                    content_origin: None,
                    activity_type: None,
                    is_self_generated: false,
                    evidence_strength: None,
                    app_name: None,
                    win_title: None,
                    category: None,
                    user_verified: false,
                };

                if let Err(e) = storage.insert_vector_index(&index) {
                    warn!(capture_id, "写入向量索引元数据失败: {}", e);
                } else {
                    debug!(capture_id, "向量索引元数据已写入");
                }
            }
            Err(e) => {
                warn!(capture_id, "Embedding 调用失败: {}", e);
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 测试
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::capture::screenshot::{clear_test_screenshots, push_test_screenshot_from_image};
    use crate::storage::repo::capture::CaptureFilter;
    use crate::storage::StorageManager;
    use image::{DynamicImage, GrayImage, Luma};
    use rusqlite::Connection;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    fn make_test_captures_dir() -> PathBuf {
        static COUNTER: AtomicUsize = AtomicUsize::new(0);
        let suffix = COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "memory-bread-test-captures-{}-{}",
            current_ts_ms(),
            suffix
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// 创建测试用引擎（关闭截图和 AX，全部走 mock）
    fn make_engine() -> CaptureEngine {
        let storage = StorageManager::open_in_memory().unwrap();
        let config = CaptureConfig {
            captures_dir: make_test_captures_dir(),
            enable_screenshot: false,
            enable_ax: false,
            ..Default::default()
        };
        let mut engine = CaptureEngine::new(storage, config);
        engine.ipc_client = None;
        engine
    }

    fn make_engine_with_screenshot() -> CaptureEngine {
        let storage = StorageManager::open_in_memory().unwrap();
        let config = CaptureConfig {
            captures_dir: make_test_captures_dir(),
            enable_screenshot: true,
            enable_ax: false,
            ..Default::default()
        };
        let mut engine = CaptureEngine::new(storage, config);
        engine.ipc_client = None;
        engine
    }

    fn make_minimal_privacy_engine() -> CaptureEngine {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                app_name TEXT,
                app_bundle_id TEXT,
                win_title TEXT,
                event_type TEXT NOT NULL DEFAULT 'auto',
                ax_text TEXT,
                ax_focused_role TEXT,
                ax_focused_id TEXT,
                ocr_text TEXT,
                screenshot_path TEXT,
                audio_text TEXT,
                input_text TEXT,
                is_sensitive INTEGER NOT NULL DEFAULT 0,
                pii_scrubbed INTEGER NOT NULL DEFAULT 0,
                screenshot_source TEXT,
                url TEXT,
                webpage_title TEXT
            );
            CREATE TABLE privacy_filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filter_type TEXT NOT NULL UNIQUE,
                filter_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                config_json TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app_blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bundle_id TEXT NOT NULL UNIQUE,
                app_name TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE privacy_block_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stat_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                block_count INTEGER NOT NULL DEFAULT 0,
                week_start TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(stat_type, target_id, week_start)
            );
            "#,
        )
        .unwrap();
        conn.execute_batch(include_str!(
            "../storage/migrations/035_seed_privacy_defaults.sql"
        ))
        .unwrap();
        conn.execute_batch(include_str!(
            "../storage/migrations/059_seed_other_privacy_filter.sql"
        ))
        .unwrap();

        let storage = StorageManager {
            conn: Arc::new(Mutex::new(conn)),
        };
        let config = CaptureConfig {
            captures_dir: make_test_captures_dir(),
            enable_screenshot: false,
            enable_ax: false,
            ..Default::default()
        };
        let mut engine = CaptureEngine::new(storage, config);
        engine.ipc_client = None;
        engine
    }

    fn gradient_image(offset: u8) -> DynamicImage {
        let mut image = GrayImage::new(64, 64);
        for y in 0..64 {
            for x in 0..64 {
                let value = x as u8 ^ offset ^ ((y as u8) >> 2);
                image.put_pixel(x, y, Luma([value]));
            }
        }
        DynamicImage::ImageLuma8(image)
    }

    // ── 单事件处理 ────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_mouse_click_without_payload_skips_insert() {
        let engine = make_engine();
        let result = engine
            .process_event(CaptureEvent::MouseClick { x: 100.0, y: 200.0 })
            .await
            .unwrap();

        assert!(result.is_none());
        let list = engine.storage.list_captures(&CaptureFilter::new()).unwrap();
        assert!(list.is_empty());
    }

    #[tokio::test]
    async fn test_loginwindow_context_is_not_persisted_and_clears_cached_app() {
        let engine = make_engine();
        engine.update_cached_context(&AXInfo {
            app_name: Some("Code".into()),
            app_bundle_id: Some("com.microsoft.VSCode".into()),
            ..Default::default()
        });

        let result = engine
            .process_event(CaptureEvent::AppSwitch {
                app_name: "loginwindow".into(),
                bundle_id: Some("com.apple.loginwindow".into()),
                win_title: String::new(),
            })
            .await
            .unwrap();

        assert!(result.is_none());
        assert!(engine
            .last_context
            .lock()
            .expect("cached context lock")
            .is_none());
        assert!(engine
            .storage
            .list_captures(&CaptureFilter::new())
            .unwrap()
            .is_empty());

        engine.update_cached_context(&AXInfo {
            app_name: Some("Code".into()),
            app_bundle_id: Some("com.microsoft.VSCode".into()),
            ..Default::default()
        });
        let periodic_result = engine
            .process_event_with_ax_info(
                CaptureEvent::Periodic,
                Some(AXInfo {
                    app_name: Some("loginwindow".into()),
                    app_bundle_id: Some("com.apple.loginwindow".into()),
                    ..Default::default()
                }),
                false,
            )
            .await
            .unwrap();
        assert!(periodic_result.is_none());
        assert!(engine
            .last_context
            .lock()
            .expect("cached context lock")
            .is_none());
    }

    #[tokio::test]
    async fn test_key_pause_without_visual_payload_skips_insert() {
        let engine = make_engine();
        let result = engine.process_event(CaptureEvent::KeyPause).await.unwrap();

        assert!(result.is_none());
        assert!(engine
            .storage
            .list_captures(&CaptureFilter::new())
            .unwrap()
            .is_empty());
    }

    #[test]
    fn test_save_capture_redacts_configured_sensitive_text() {
        let engine = make_minimal_privacy_engine();
        let ax = AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("普通网页".into()),
            extracted_text: Some("客户手机号 13800138000，另有色情信息".into()),
            ..Default::default()
        };
        let event = CaptureEvent::Manual;

        let id = engine
            .save_capture(
                current_ts_ms(),
                &ax,
                &event,
                None,
                None,
                Some("备用手机号 13900139000".into()),
                false,
            )
            .unwrap();

        let rec = engine.storage.get_capture(id).unwrap().unwrap();
        assert_eq!(
            rec.ax_text.as_deref(),
            Some("客户手机号 [已过滤]，另有[已过滤]")
        );
        assert!(rec.input_text.is_none());
        assert_eq!(rec.ocr_text.as_deref(), Some("备用手机号 [已过滤]"));
        assert!(rec.pii_scrubbed);

        let stats = engine
            .storage
            .with_conn(crate::storage::repo::privacy::get_week_block_stats)
            .unwrap();
        assert!(stats
            .iter()
            .any(|stat| stat.stat_type == "filter" && stat.target_id == "pii"));
        assert!(stats
            .iter()
            .any(|stat| stat.stat_type == "filter" && stat.target_id == "other"));
    }

    #[tokio::test]
    async fn test_app_switch_full_capture_stores_ax_text_without_screenshot() {
        let engine = make_engine_with_screenshot();
        let event = CaptureEvent::AppSwitch {
            app_name: "Feishu".into(),
            bundle_id: Some("com.feishu.feishu".into()),
            win_title: "工作群".into(),
        };
        let id = engine
            .process_event_with_ax_info(
                event,
                Some(AXInfo {
                    app_name: Some("Feishu".into()),
                    app_bundle_id: Some("com.feishu.feishu".into()),
                    win_title: Some("工作群".into()),
                    extracted_text: Some("项目同步中".into()),
                    ..Default::default()
                }),
                false,
            )
            .await
            .unwrap()
            .unwrap();

        let record = engine.storage.get_capture(id).unwrap().unwrap();
        assert_eq!(record.event_type, "app_switch");
        assert_eq!(record.app_name.as_deref(), Some("Feishu"));
        assert_eq!(record.win_title.as_deref(), Some("工作群"));
        assert!(record.screenshot_path.is_none());
        assert_eq!(record.ax_text.as_deref(), Some("项目同步中"));
        assert!(record.ocr_text.is_none());
    }

    #[tokio::test]
    async fn test_browser_navigation_full_capture_stores_ax_text_without_screenshot() {
        let engine = make_engine_with_screenshot();
        let event = CaptureEvent::BrowserNavigation {
            app_name: "Google Chrome".into(),
            bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Example".into()),
            url: "https://example.com/page".into(),
            webpage_title: Some("Example".into()),
        };
        let id = engine
            .process_event_with_ax_info(
                event,
                Some(AXInfo {
                    app_name: Some("Google Chrome".into()),
                    app_bundle_id: Some("com.google.Chrome".into()),
                    win_title: Some("Example".into()),
                    url: Some("https://example.com/page".into()),
                    webpage_title: Some("Example".into()),
                    extracted_text: Some("Example 正文".into()),
                    ..Default::default()
                }),
                false,
            )
            .await
            .unwrap()
            .unwrap();

        let record = engine.storage.get_capture(id).unwrap().unwrap();
        assert_eq!(record.event_type, "browser_navigation");
        assert_eq!(record.url.as_deref(), Some("https://example.com/page"));
        assert_eq!(record.webpage_title.as_deref(), Some("Example"));
        assert_eq!(record.ax_text.as_deref(), Some("Example 正文"));
        assert!(record.screenshot_path.is_none());
        assert!(record.ocr_text.is_none());
    }

    #[tokio::test]
    async fn test_browser_navigation_without_ax_text_uses_screenshot_for_ocr() {
        clear_test_screenshots();
        let engine = make_engine_with_screenshot();
        push_test_screenshot_from_image(&gradient_image(0));
        let id = engine
            .process_event_with_ax_info(
                CaptureEvent::BrowserNavigation {
                    app_name: "Google Chrome".into(),
                    bundle_id: Some("com.google.Chrome".into()),
                    win_title: Some("Canvas App".into()),
                    url: "https://example.com/canvas".into(),
                    webpage_title: Some("Canvas App".into()),
                },
                Some(AXInfo {
                    app_name: Some("Google Chrome".into()),
                    app_bundle_id: Some("com.google.Chrome".into()),
                    win_title: Some("Canvas App".into()),
                    url: Some("https://example.com/canvas".into()),
                    webpage_title: Some("Canvas App".into()),
                    ..Default::default()
                }),
                false,
            )
            .await
            .unwrap()
            .unwrap();

        let rec = engine.storage.get_capture(id).unwrap().unwrap();
        assert_eq!(rec.event_type, "browser_navigation");
        assert!(rec.ax_text.is_none());
        assert!(rec.screenshot_path.is_some());
        assert!(rec.ocr_text.is_none(), "OCR 应后台补写，不阻塞变化采集");
    }

    #[tokio::test]
    async fn test_explicit_signal_uses_visual_probe_before_full_capture() {
        clear_test_screenshots();
        let engine = make_engine_with_screenshot();
        let event = || CaptureEvent::AppSwitch {
            app_name: "Chrome".into(),
            bundle_id: Some("com.google.Chrome".into()),
            win_title: "Doc".into(),
        };

        push_test_screenshot_from_image(&gradient_image(0));
        let first_id = engine.process_event(event()).await.unwrap().unwrap();
        assert!(engine
            .storage
            .get_capture(first_id)
            .unwrap()
            .unwrap()
            .screenshot_path
            .is_some());

        push_test_screenshot_from_image(&gradient_image(0));
        assert!(engine.process_event(event()).await.unwrap().is_none());
        let deduplicated: i64 = engine
            .storage
            .with_conn(|conn| {
                Ok(conn.query_row(
                    "SELECT COUNT(*) FROM capture_attempts
                     WHERE outcome = 'deduplicated'
                       AND reason = 'visual_fingerprint_below_threshold'",
                    [],
                    |row| row.get(0),
                )?)
            })
            .unwrap();
        assert_eq!(deduplicated, 1);

        tokio::time::sleep(Duration::from_millis(2)).await;
        push_test_screenshot_from_image(&gradient_image(255));
        assert!(engine.process_event(event()).await.unwrap().is_some());
        assert_eq!(
            engine
                .storage
                .list_captures(&CaptureFilter::new())
                .unwrap()
                .len(),
            2
        );
    }

    #[tokio::test]
    async fn test_stale_browser_navigation_is_dropped_before_insert() {
        let engine = make_engine_with_screenshot();
        let result = engine
            .process_event_with_ax_info(
                CaptureEvent::BrowserNavigation {
                    app_name: "Google Chrome".into(),
                    bundle_id: Some("com.google.Chrome".into()),
                    win_title: Some("Old Page".into()),
                    url: "https://example.com/old".into(),
                    webpage_title: Some("Old Page".into()),
                },
                Some(AXInfo {
                    app_name: Some("Google Chrome".into()),
                    app_bundle_id: Some("com.google.Chrome".into()),
                    win_title: Some("New Page".into()),
                    url: Some("https://example.com/new".into()),
                    extracted_text: Some("新页面正文".into()),
                    ..Default::default()
                }),
                false,
            )
            .await
            .unwrap();

        assert!(result.is_none());
        assert!(engine
            .storage
            .list_captures(&CaptureFilter::new())
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn test_scroll_without_payload_skips_insert() {
        let engine = make_engine();
        let result = engine.process_event(CaptureEvent::Scroll).await.unwrap();

        assert!(result.is_none());
        let list = engine.storage.list_captures(&CaptureFilter::new()).unwrap();
        assert!(list.is_empty());
    }

    #[tokio::test]
    async fn test_periodic_event() {
        let engine = make_engine();
        let result = engine.process_event(CaptureEvent::Periodic).await.unwrap();

        assert!(result.is_none());
        let list = engine.storage.list_captures(&CaptureFilter::new()).unwrap();
        assert!(list.is_empty());
    }

    #[tokio::test]
    async fn test_periodic_with_ax_text_skips_screenshot() {
        let engine = make_engine_with_screenshot();
        let ax = AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Doc".into()),
            extracted_text: Some("这是一段 AX 正文".into()),
            ..Default::default()
        };

        let id = engine
            .process_event_with_ax_info(CaptureEvent::Periodic, Some(ax), false)
            .await
            .unwrap()
            .unwrap();
        let rec = engine.storage.get_capture(id).unwrap().unwrap();
        assert_eq!(rec.event_type, "auto");
        assert!(rec.screenshot_path.is_none());
        assert_eq!(rec.ax_text.as_deref(), Some("这是一段 AX 正文"));
    }

    #[tokio::test]
    async fn test_periodic_ax_missing_without_screenshot_skips_insert() {
        let engine = make_engine();
        engine.update_cached_context(&AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Doc".into()),
            ..Default::default()
        });

        let result = engine.process_event(CaptureEvent::Periodic).await.unwrap();
        assert!(result.is_none());

        let list = engine.storage.list_captures(&CaptureFilter::new()).unwrap();
        assert!(list.is_empty());
    }

    #[tokio::test]
    async fn test_periodic_ax_missing_with_new_frame_inserts_capture_and_keeps_screenshot() {
        clear_test_screenshots();
        let engine = make_engine_with_screenshot();
        engine.update_cached_context(&AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Doc".into()),
            ..Default::default()
        });
        push_test_screenshot_from_image(&gradient_image(0));

        let id = engine
            .process_event(CaptureEvent::Periodic)
            .await
            .unwrap()
            .unwrap();

        let rec = engine.storage.get_capture(id).unwrap().unwrap();
        assert_eq!(rec.event_type, "auto");
        assert!(rec.screenshot_path.is_some());
        assert!(rec.ax_text.is_none());
        assert!(rec.ocr_text.is_none(), "OCR 应后台补写，不阻塞本轮采集");
    }

    #[tokio::test]
    async fn test_periodic_ax_missing_duplicate_frame_keeps_continuity_and_reuses_file_block() {
        clear_test_screenshots();
        let engine = make_engine_with_screenshot();
        engine.update_cached_context(&AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Doc".into()),
            ..Default::default()
        });
        let screenshot_dir = engine.config.captures_dir.join("screenshots");
        let baseline_files = std::fs::read_dir(&screenshot_dir)
            .ok()
            .map(|entries| entries.filter_map(Result::ok).count())
            .unwrap_or(0);
        push_test_screenshot_from_image(&gradient_image(0));
        let first_id = engine
            .process_event(CaptureEvent::Periodic)
            .await
            .unwrap()
            .unwrap();
        let first = engine.storage.get_capture(first_id).unwrap().unwrap();
        let first_path = engine
            .config
            .captures_dir
            .join(first.screenshot_path.as_deref().unwrap());
        assert!(first_path.exists());

        push_test_screenshot_from_image(&gradient_image(0));
        let second_id = engine
            .process_event(CaptureEvent::Periodic)
            .await
            .unwrap()
            .unwrap();

        let list = engine.storage.list_captures(&CaptureFilter::new()).unwrap();
        assert_eq!(list.len(), 2);
        let second = engine.storage.get_capture(second_id).unwrap().unwrap();
        let second_path = engine
            .config
            .captures_dir
            .join(second.screenshot_path.as_deref().unwrap());
        assert!(second_path.exists());
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            assert_eq!(
                std::fs::metadata(&first_path).unwrap().ino(),
                std::fs::metadata(&second_path).unwrap().ino(),
                "重复帧应通过硬链接复用同一个数据块"
            );
        }

        let continuity_count: i64 = engine
            .storage
            .with_conn(|conn| {
                Ok(conn.query_row(
                    "SELECT COUNT(*) FROM capture_attempts
                     WHERE capture_id = ?1 AND outcome = 'continuity'
                       AND reason = 'periodic_visual_duplicate'",
                    rusqlite::params![second_id],
                    |row| row.get(0),
                )?)
            })
            .unwrap();
        assert_eq!(continuity_count, 1);

        let screenshot_files = std::fs::read_dir(&screenshot_dir)
            .ok()
            .map(|entries| entries.filter_map(Result::ok).count())
            .unwrap_or(0);
        assert_eq!(
            screenshot_files,
            baseline_files + 2,
            "连续性记录保留独立路径，但底层数据块必须复用"
        );
    }

    #[test]
    fn test_periodic_dedup_distance_threshold() {
        let engine = make_engine_with_screenshot();
        let info = AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Doc".into()),
            ..Default::default()
        };
        let now = current_ts_ms();

        engine.record_recent_capture(
            &info,
            RecentCaptureFingerprint {
                ts_ms: now,
                capture_id: 1,
                ax_text_hash: None,
                dhash: Some(0),
                screenshot_path: Some("screenshots/a.webp".into()),
            },
        );
        assert!(engine
            .find_recent_duplicate(&info, now, None, Some(1))
            .is_some());
        assert!(engine
            .find_recent_duplicate(&info, now, None, Some(3))
            .is_none());
    }

    #[test]
    fn test_periodic_dedup_expired_frame_no_longer_skips() {
        let engine = make_engine_with_screenshot();
        let info = AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Doc".into()),
            ..Default::default()
        };
        let now = current_ts_ms();

        engine.record_recent_capture(
            &info,
            RecentCaptureFingerprint {
                ts_ms: now - CAPTURE_DEDUP_WINDOW_MS - 10,
                capture_id: 1,
                ax_text_hash: None,
                dhash: Some(0),
                screenshot_path: Some("screenshots/a.webp".into()),
            },
        );
        assert!(engine
            .find_recent_duplicate(&info, now, None, Some(0))
            .is_none());
    }

    #[test]
    fn test_periodic_dedup_different_scene_key_does_not_collide() {
        let engine = make_engine_with_screenshot();
        let chrome = AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Doc".into()),
            ..Default::default()
        };
        let safari = AXInfo {
            app_name: Some("Safari".into()),
            app_bundle_id: Some("com.apple.Safari".into()),
            win_title: Some("Doc".into()),
            ..Default::default()
        };
        let now = current_ts_ms();

        engine.record_recent_capture(
            &chrome,
            RecentCaptureFingerprint {
                ts_ms: now,
                capture_id: 1,
                ax_text_hash: None,
                dhash: Some(0),
                screenshot_path: Some("screenshots/a.webp".into()),
            },
        );
        assert!(engine
            .find_recent_duplicate(&safari, now, None, Some(0))
            .is_none());
    }

    #[test]
    fn test_ocr_backpressure_is_visible_in_metrics() {
        let metrics = OcrBackfillMetrics::new();
        let now = current_ts_ms();
        metrics.record_skipped_without_queue(OcrBackfillOutcome::SkippedBackpressure, now);

        let snapshot = metrics.snapshot(60_000, now);
        assert_eq!(snapshot.submitted_total, 1);
        assert_eq!(snapshot.completed_total, 1);
        assert_eq!(snapshot.skipped_backpressure_total, 1);
        assert_eq!(snapshot.period_skipped_backpressure, 1);
        assert_eq!(snapshot.recent[0].status, "skipped_backpressure");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn test_ocr_timeout_sends_exactly_one_ipc_request() {
        use std::io::Read;
        use std::os::unix::net::UnixListener;
        use std::sync::atomic::AtomicUsize;

        let socket_path = std::env::temp_dir().join(format!(
            "memory-bread-ocr-timeout-{}-{}.sock",
            std::process::id(),
            current_ts_ms()
        ));
        let listener = UnixListener::bind(&socket_path).unwrap();
        let accepted = Arc::new(AtomicUsize::new(0));
        let accepted_for_server = accepted.clone();
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            accepted_for_server.fetch_add(1, Ordering::SeqCst);
            let mut length = [0u8; 4];
            stream.read_exact(&mut length).unwrap();
            let mut payload = vec![0u8; u32::from_be_bytes(length) as usize];
            stream.read_exact(&mut payload).unwrap();
            std::thread::sleep(Duration::from_millis(120));
        });

        let client = IpcClient::with_socket_path_and_timeout(
            socket_path.to_string_lossy(),
            Duration::from_millis(40),
        );
        let result = run_ocr_backfill(client, "/tmp/one-shot.jpg".to_string()).await;

        assert!(result.is_err());
        server.join().unwrap();
        assert_eq!(accepted.load(Ordering::SeqCst), 1);
        let _ = std::fs::remove_file(socket_path);
    }

    // ── 隐私过滤 ──────────────────────────────────────────────────────────

    #[tokio::test]
    async fn test_blocked_app_is_dropped_before_capture() {
        let storage = StorageManager::open_in_memory().unwrap();
        let config = CaptureConfig {
            enable_screenshot: false,
            enable_ax: false,
            ..Default::default()
        };
        let filter = PrivacyFilter::new().with_extra_blocked_apps(&["SecretApp".into()]);
        let engine = CaptureEngine::with_filter(storage, config, filter);

        let result = engine
            .process_event(CaptureEvent::AppSwitch {
                app_name: "SecretApp".into(),
                bundle_id: None,
                win_title: "Secret Window".into(),
            })
            .await
            .unwrap();

        assert!(result.is_none());
        assert!(engine
            .storage
            .list_captures(&CaptureFilter::new())
            .unwrap()
            .is_empty());
        let private_attempt: (i64, Option<String>, Option<String>, String) = engine
            .storage
            .with_conn(|conn| {
                Ok(conn.query_row(
                    "SELECT is_private, app_name, win_title, reason
                     FROM capture_attempts ORDER BY id DESC LIMIT 1",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )?)
            })
            .unwrap();
        assert_eq!(private_attempt.0, 1);
        assert_eq!(private_attempt.1, None);
        assert_eq!(private_attempt.2, None);
        assert_eq!(private_attempt.3, "sensitive_context");
    }

    #[tokio::test]
    async fn test_wechat_blocked_app_is_dropped_before_capture() {
        let storage = StorageManager::open_in_memory().unwrap();
        let config = CaptureConfig {
            enable_screenshot: false,
            enable_ax: false,
            ..Default::default()
        };
        let filter = PrivacyFilter::new().with_extra_blocked_apps(&["WeChat".into()]);
        let engine = CaptureEngine::with_filter(storage, config, filter);

        let result = engine
            .process_event(CaptureEvent::AppSwitch {
                app_name: "WeChat".into(),
                bundle_id: Some("com.tencent.xinWeChat".into()),
                win_title: "微信聊天".into(),
            })
            .await
            .unwrap();

        assert!(result.is_none());
        assert!(engine
            .storage
            .list_captures(&CaptureFilter::new())
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn test_default_blocked_app_1password() {
        let storage = StorageManager::open_in_memory().unwrap();
        let config = CaptureConfig {
            enable_screenshot: false,
            enable_ax: false,
            ..Default::default()
        };
        let engine = CaptureEngine::new(storage, config);

        let result = engine
            .process_event(CaptureEvent::AppSwitch {
                app_name: "1Password".into(),
                bundle_id: None,
                win_title: "Unlock 1Password".into(),
            })
            .await
            .unwrap();

        assert!(result.is_none());
        assert!(engine
            .storage
            .list_captures(&CaptureFilter::new())
            .unwrap()
            .is_empty());
    }

    // ── channel 事件循环 ──────────────────────────────────────────────────

    #[tokio::test]
    async fn test_run_loop_processes_multiple_events() {
        let storage = StorageManager::open_in_memory().unwrap();
        let storage_clone = storage.clone();

        let config = CaptureConfig {
            enable_screenshot: false,
            enable_ax: false,
            ..Default::default()
        };
        let engine = CaptureEngine::new(storage, config);
        let (tx, rx) = mpsc::channel::<CaptureEvent>(16);

        // 发送 4 个无内容事件后关闭 channel，均不应产生空壳采集。
        tx.send(CaptureEvent::Manual).await.unwrap();
        tx.send(CaptureEvent::Periodic).await.unwrap();
        tx.send(CaptureEvent::Scroll).await.unwrap();
        tx.send(CaptureEvent::KeyPause).await.unwrap();
        drop(tx); // channel 关闭后 run() 返回

        engine.run(rx).await.unwrap();

        let list = storage_clone.list_captures(&CaptureFilter::new()).unwrap();
        assert!(list.is_empty(), "输入信号不得生成没有视觉或文本内容的记录");
    }

    // ── CaptureEvent 方法 ─────────────────────────────────────────────────

    #[test]
    fn test_event_to_event_type_mapping() {
        use crate::storage::models::EventType;
        assert_eq!(CaptureEvent::Periodic.to_event_type(), EventType::Auto);
        assert_eq!(CaptureEvent::Manual.to_event_type(), EventType::Manual);
        assert_eq!(CaptureEvent::Scroll.to_event_type(), EventType::Scroll);
        assert_eq!(
            CaptureEvent::BrowserNavigation {
                app_name: "Chrome".into(),
                bundle_id: Some("com.google.Chrome".into()),
                win_title: Some("Example".into()),
                url: "https://example.com".into(),
                webpage_title: Some("Example".into()),
            }
            .to_event_type(),
            EventType::BrowserNavigation
        );
        assert_eq!(
            CaptureEvent::MouseClick { x: 0.0, y: 0.0 }.to_event_type(),
            EventType::MouseClick
        );
        assert_eq!(CaptureEvent::KeyPause.to_event_type(), EventType::KeyPause);
        assert_eq!(
            CaptureEvent::AppSwitch {
                app_name: "".into(),
                bundle_id: None,
                win_title: "".into()
            }
            .to_event_type(),
            EventType::AppSwitch
        );
    }

    #[test]
    fn test_event_input_text() {
        let e1 = CaptureEvent::KeyPause;
        assert!(e1.input_text().is_none());
        assert!(!has_meaningful_input_text(&e1));

        let e2 = CaptureEvent::Manual;
        assert!(e2.input_text().is_none());
        assert!(!has_meaningful_input_text(&e2));

        let e3 = CaptureEvent::MouseClick { x: 1.0, y: 2.0 };
        assert!(e3.input_text().is_none());
        assert!(!has_meaningful_input_text(&e3));
    }

    #[test]
    fn test_event_app_name() {
        let e = CaptureEvent::AppSwitch {
            app_name: "Chrome".into(),
            bundle_id: None,
            win_title: "Google".into(),
        };
        assert_eq!(e.app_name(), Some("Chrome"));
        assert!(CaptureEvent::Manual.app_name().is_none());
    }

    // ── merge_ax_and_event ────────────────────────────────────────────────

    #[test]
    fn test_merge_uses_ax_for_non_app_switch() {
        let engine = make_engine();
        let ax_info = AXInfo {
            app_name: Some("Chrome".into()),
            win_title: Some("Google Search".into()),
            ..Default::default()
        };
        let merged = engine.merge_ax_and_event(&CaptureEvent::Manual, Some(ax_info));
        assert_eq!(merged.app_name.as_deref(), Some("Chrome"));
        assert_eq!(merged.win_title.as_deref(), Some("Google Search"));
    }

    #[test]
    fn test_merge_falls_back_to_cached_context() {
        let engine = make_engine();
        engine.update_cached_context(&AXInfo {
            app_name: Some("Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Docs".into()),
            ..Default::default()
        });

        let merged = engine.merge_ax_and_event(&CaptureEvent::Periodic, None);
        assert_eq!(merged.app_name.as_deref(), Some("Chrome"));
        assert_eq!(merged.app_bundle_id.as_deref(), Some("com.google.Chrome"));
        assert_eq!(merged.win_title.as_deref(), Some("Docs"));
    }

    #[test]
    fn test_merge_prefers_current_ax_over_cached_context() {
        let engine = make_engine();
        engine.update_cached_context(&AXInfo {
            app_name: Some("OldApp".into()),
            win_title: Some("Old Window".into()),
            ..Default::default()
        });

        let merged = engine.merge_ax_and_event(
            &CaptureEvent::Periodic,
            Some(AXInfo {
                app_name: Some("NewApp".into()),
                win_title: Some("New Window".into()),
                ..Default::default()
            }),
        );
        assert_eq!(merged.app_name.as_deref(), Some("NewApp"));
        assert_eq!(merged.win_title.as_deref(), Some("New Window"));
    }

    #[test]
    fn test_merge_does_not_mix_cache_across_apps() {
        let engine = make_engine();
        engine.update_cached_context(&AXInfo {
            app_name: Some("Google Chrome".into()),
            app_bundle_id: Some("com.google.Chrome".into()),
            win_title: Some("Old Browser Page".into()),
            ..Default::default()
        });

        let merged = engine.merge_ax_and_event(
            &CaptureEvent::Periodic,
            Some(AXInfo {
                app_name: Some("memory-bread-desktop".into()),
                win_title: Some("MemoryBread".into()),
                ..Default::default()
            }),
        );
        assert_eq!(merged.app_name.as_deref(), Some("memory-bread-desktop"));
        assert!(merged.app_bundle_id.is_none());
        assert_eq!(merged.win_title.as_deref(), Some("MemoryBread"));
    }
}
