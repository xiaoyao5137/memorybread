//! 共享应用状态（通过 axum State extractor 注入每个 handler）

use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
};

use crate::{
    browser_extension::BrowserExtensionBroker, capture::CaptureSchedule, storage::StorageManager,
};
use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Clone)]
pub struct DebugLogSpec {
    pub key: String,
    pub label: String,
    pub dir: PathBuf,
    pub file_name: String,
}

impl DebugLogSpec {
    pub fn new(
        key: impl Into<String>,
        label: impl Into<String>,
        dir: PathBuf,
        file_name: impl Into<String>,
    ) -> Self {
        Self {
            key: key.into(),
            label: label.into(),
            dir,
            file_name: file_name.into(),
        }
    }

    pub fn path(&self) -> PathBuf {
        self.dir.join(&self.file_name)
    }
}

fn default_debug_log_specs() -> Vec<DebugLogSpec> {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let log_dir = PathBuf::from(home).join(".memory-bread").join("logs");

    vec![
        DebugLogSpec::new(
            "core",
            "core.log · Core Engine",
            log_dir.clone(),
            "core.log",
        ),
        DebugLogSpec::new(
            "sidecar",
            "sidecar.log · AI Sidecar",
            log_dir.clone(),
            "sidecar.log",
        ),
        DebugLogSpec::new(
            "model_api",
            "model_api.log · Model API",
            log_dir.clone(),
            "model_api.log",
        ),
        DebugLogSpec::new(
            "creation",
            "creation.log · Creation Service",
            log_dir.clone(),
            "creation.log",
        ),
        DebugLogSpec::new(
            "bake_extract_errors",
            "bake_extract_errors.log · Bake 提炼错误",
            log_dir.clone(),
            "bake_extract_errors.log",
        ),
        DebugLogSpec::new("ui", "ui.log · Desktop UI", log_dir, "ui.log"),
    ]
}

#[derive(Debug, Clone, Serialize)]
pub struct RagJobRecord {
    pub id: String,
    pub status: String,
    pub result: Option<Value>,
    pub error: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, Serialize)]
pub struct CreationSkillAnalysisJobRecord {
    pub id: String,
    pub status: String,
    pub result: Option<Value>,
    pub error_code: Option<String>,
    pub error: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone)]
pub struct CreationSessionLease {
    pub owner_request_id: String,
    pub updated_at_ms: i64,
}

/// 所有 Handler 共享的应用状态。
///
/// 使用 `Arc<AppState>` 确保零拷贝跨线程共享。
#[derive(Clone)]
pub struct AppState {
    pub storage: StorageManager,
    pub sidecar_url: String,
    pub creation_sidecar_url: String,
    pub debug_log_specs: Vec<DebugLogSpec>,
    pub rag_jobs: Arc<Mutex<HashMap<String, RagJobRecord>>>,
    pub rag_job_seq: Arc<AtomicU64>,
    pub creation_skill_analysis_jobs: Arc<Mutex<HashMap<String, CreationSkillAnalysisJobRecord>>>,
    pub creation_skill_analysis_job_seq: Arc<AtomicU64>,
    pub creation_session_leases: Arc<Mutex<HashMap<String, CreationSessionLease>>>,
    pub capture_enabled: Arc<AtomicBool>,
    pub keyboard_signal_enabled: Arc<AtomicBool>,
    pub capture_schedule: Arc<CaptureSchedule>,
    pub browser_extension: BrowserExtensionBroker,
}

impl AppState {
    pub fn new(storage: StorageManager) -> Arc<Self> {
        let sidecar_url =
            std::env::var("SIDECAR_URL").unwrap_or_else(|_| "http://127.0.0.1:7071".to_string());
        let creation_sidecar_url = std::env::var("CREATION_SIDECAR_URL")
            .unwrap_or_else(|_| "http://127.0.0.1:8001".to_string());
        Self::with_service_urls(
            storage,
            sidecar_url,
            creation_sidecar_url,
            default_debug_log_specs(),
        )
    }

    pub fn with_config(
        storage: StorageManager,
        sidecar_url: String,
        debug_log_specs: Vec<DebugLogSpec>,
    ) -> Arc<Self> {
        let creation_sidecar_url = std::env::var("CREATION_SIDECAR_URL")
            .unwrap_or_else(|_| "http://127.0.0.1:8001".to_string());
        Self::with_service_urls(storage, sidecar_url, creation_sidecar_url, debug_log_specs)
    }

    pub fn with_service_urls(
        storage: StorageManager,
        sidecar_url: String,
        creation_sidecar_url: String,
        debug_log_specs: Vec<DebugLogSpec>,
    ) -> Arc<Self> {
        match storage.fail_orphaned_running_bake_runs_on_startup() {
            Ok(count) if count > 0 => {
                tracing::warn!("启动时已收敛 {} 个孤儿 running bake run", count);
            }
            Err(error) => {
                tracing::warn!("启动时清理孤儿 bake run 失败: {}", error);
            }
            _ => {}
        }
        match storage.clear_recoverable_bake_retry_failures() {
            Ok(count) if count > 0 => {
                tracing::warn!(
                    "启动时已恢复 {} 个被瞬态上游错误或旧响应兼容问题阻塞的 bake 候选，并回退处理水位",
                    count
                );
            }
            Err(error) => {
                tracing::warn!("启动时恢复可处理 bake 候选失败: {}", error);
            }
            _ => {}
        }
        let capture_enabled = storage
            .get_preference("runtime.capture_enabled")
            .ok()
            .flatten()
            .map(|preference| preference.value != "false")
            // 新安装默认开启采集，让用户可以立即体验核心功能。
            .unwrap_or(true);
        let capture_enabled = std::env::var("MEMORY_BREAD_CAPTURE_ENABLED")
            .ok()
            .and_then(|value| match value.trim().to_ascii_lowercase().as_str() {
                "true" | "1" | "yes" | "on" => Some(true),
                "false" | "0" | "no" | "off" => Some(false),
                _ => None,
            })
            .unwrap_or(capture_enabled);
        let capture_interval_secs = storage
            .get_preference("privacy.capture_interval_sec")
            .ok()
            .flatten()
            .and_then(|preference| preference.value.parse::<u64>().ok())
            .map(|value| value.clamp(5, 120))
            .unwrap_or(90);
        let keyboard_signal_enabled = storage
            .get_preference("privacy.keyboard_capture")
            .ok()
            .flatten()
            .map(|preference| preference.value.trim().eq_ignore_ascii_case("true"))
            .unwrap_or(true);

        Arc::new(Self {
            storage,
            sidecar_url,
            creation_sidecar_url,
            debug_log_specs,
            rag_jobs: Arc::new(Mutex::new(HashMap::new())),
            rag_job_seq: Arc::new(AtomicU64::new(1)),
            creation_skill_analysis_jobs: Arc::new(Mutex::new(HashMap::new())),
            creation_skill_analysis_job_seq: Arc::new(AtomicU64::new(1)),
            creation_session_leases: Arc::new(Mutex::new(HashMap::new())),
            capture_enabled: Arc::new(AtomicBool::new(capture_enabled)),
            keyboard_signal_enabled: Arc::new(AtomicBool::new(keyboard_signal_enabled)),
            capture_schedule: Arc::new(CaptureSchedule::new(capture_interval_secs)),
            browser_extension: BrowserExtensionBroker::new(),
        })
    }

    pub fn is_capture_enabled(&self) -> bool {
        self.capture_enabled.load(Ordering::Relaxed)
    }

    pub fn update_capture_interval(&self, interval_secs: u64) {
        self.capture_schedule
            .update_configured_interval(interval_secs.clamp(5, 120));
    }

    pub fn update_keyboard_signal_enabled(&self, enabled: bool) {
        self.keyboard_signal_enabled
            .store(enabled, Ordering::Relaxed);
    }
}
