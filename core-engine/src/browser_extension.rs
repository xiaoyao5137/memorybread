//! Chrome 扩展后台采集任务的本地内存编排器。
//!
//! 扩展通过 Native Messaging Bridge 轮询任务；业务 handler 只提交受限的
//! 只读采集请求，并等待同一 job_id 的结果。Core 重启后队列自然清空，旧结果
//! 无法写入新的来源快照。

use std::{
    collections::hash_map::DefaultHasher,
    collections::{HashMap, VecDeque},
    fs,
    hash::{Hash, Hasher},
    io::{BufRead, BufReader, BufWriter, Write},
    path::PathBuf,
    sync::{Arc, Mutex},
    thread,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::oneshot;
use uuid::Uuid;

use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine as _};

const CONNECTED_TTL_MS: i64 = 15_000;
const MAX_BRIDGE_MESSAGE_BYTES: usize = 1024 * 1024;
const MAX_PREVIEW_BYTES: usize = 768 * 1024;
const COMPLETED_JOB_TTL_MS: i64 = 30_000;
pub const BROWSER_BRIDGE_SOCKET_ENV: &str = "MEMORY_BREAD_BROWSER_BRIDGE_SOCKET";
const BROWSER_BRIDGE_SOCKET_MAX_BYTES: usize = 100;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

fn display_url(value: &str) -> String {
    let query_index = value.find('?').unwrap_or(value.len());
    let fragment_index = value.find('#').unwrap_or(value.len());
    value[..query_index.min(fragment_index)]
        .trim()
        .chars()
        .take(2_048)
        .collect()
}

#[derive(Debug, Clone, Serialize)]
pub struct BrowserExtensionJob {
    pub schema_version: &'static str,
    pub browser_job_id: String,
    pub url: String,
    pub objective: Option<String>,
    pub requested_metrics: Vec<String>,
    pub expected_period_start: Option<String>,
    pub expected_period_end: Option<String>,
    pub max_characters: usize,
    pub max_segments: usize,
    pub deadline_ms: i64,
    pub focus_policy: &'static str,
}

impl BrowserExtensionJob {
    pub fn new(
        url: String,
        objective: Option<String>,
        requested_metrics: Vec<String>,
        expected_period_start: Option<String>,
        expected_period_end: Option<String>,
        timeout: Duration,
    ) -> Self {
        Self {
            schema_version: "memorybread.browser-job.v1",
            browser_job_id: Uuid::new_v4().to_string(),
            url,
            objective,
            requested_metrics,
            expected_period_start,
            expected_period_end,
            max_characters: 80_000,
            max_segments: 20,
            deadline_ms: now_ms() + timeout.as_millis() as i64,
            focus_policy: "never",
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BrowserExtensionResult {
    pub browser_job_id: String,
    pub status: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub content_text: String,
    #[serde(default)]
    pub structured_data: Value,
    #[serde(default)]
    pub completeness: Value,
    #[serde(default)]
    pub error_code: Option<String>,
    #[serde(default)]
    pub error_message: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BrowserExtensionStatus {
    pub schema_version: &'static str,
    pub connected: bool,
    pub extension_version: Option<String>,
    pub last_seen_at: Option<i64>,
    pub active_job_count: usize,
    pub queued_job_count: usize,
    pub jobs: Vec<BrowserLiveJobView>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BrowserLiveJobView {
    pub browser_job_id: String,
    pub url: String,
    pub title: String,
    pub status: String,
    pub stage: String,
    pub updated_at: i64,
    pub has_preview: bool,
    pub preview_revision: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BrowserExtensionProgress {
    pub browser_job_id: String,
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub stage: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub url: String,
    #[serde(default)]
    pub preview_base64: Option<String>,
    #[serde(default)]
    pub preview_mime_type: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct BrowserBridgeMessage {
    #[serde(rename = "type")]
    pub message_type: String,
    #[serde(default)]
    pub extension_version: Option<String>,
    #[serde(default)]
    pub result: Option<BrowserExtensionResult>,
    #[serde(default)]
    pub progress: Option<BrowserExtensionProgress>,
}

#[derive(Debug, Serialize)]
pub struct BrowserBridgeResponse {
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub job: Option<BrowserExtensionJob>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<&'static str>,
}

#[derive(Debug)]
pub enum BrowserExtensionError {
    Unavailable,
    Timeout,
    Failed(String, String),
    Internal,
}

struct BrokerState {
    queue: VecDeque<BrowserExtensionJob>,
    pending: HashMap<String, oneshot::Sender<BrowserExtensionResult>>,
    extension_version: Option<String>,
    last_seen_at: Option<i64>,
    live_jobs: HashMap<String, BrowserLiveJob>,
}

struct BrowserLiveJob {
    view: BrowserLiveJobView,
    preview_mime_type: Option<String>,
    preview_bytes: Option<Vec<u8>>,
}

#[derive(Clone)]
pub struct BrowserExtensionBroker {
    state: Arc<Mutex<BrokerState>>,
}

impl BrowserExtensionBroker {
    pub fn new() -> Self {
        Self {
            state: Arc::new(Mutex::new(BrokerState {
                queue: VecDeque::new(),
                pending: HashMap::new(),
                extension_version: None,
                last_seen_at: None,
                live_jobs: HashMap::new(),
            })),
        }
    }

    pub fn heartbeat(&self, extension_version: Option<String>) {
        if let Ok(mut state) = self.state.lock() {
            state.last_seen_at = Some(now_ms());
            if extension_version
                .as_deref()
                .is_some_and(|value| !value.trim().is_empty())
            {
                state.extension_version = extension_version;
            }
        }
    }

    pub fn status(&self) -> BrowserExtensionStatus {
        let Ok(mut state) = self.state.lock() else {
            return BrowserExtensionStatus {
                schema_version: "memorybread.browser-extension-status.v1",
                connected: false,
                extension_version: None,
                last_seen_at: None,
                active_job_count: 0,
                queued_job_count: 0,
                jobs: Vec::new(),
            };
        };
        let current_time = now_ms();
        state.live_jobs.retain(|_, job| {
            job.view.status == "queued"
                || job.view.status == "running"
                || current_time.saturating_sub(job.view.updated_at) <= COMPLETED_JOB_TTL_MS
        });
        let connected = state
            .last_seen_at
            .is_some_and(|last_seen| current_time.saturating_sub(last_seen) <= CONNECTED_TTL_MS);
        let mut jobs = state
            .live_jobs
            .values()
            .map(|job| job.view.clone())
            .collect::<Vec<_>>();
        jobs.sort_by_key(|job| job.updated_at);
        jobs.reverse();
        BrowserExtensionStatus {
            schema_version: "memorybread.browser-extension-status.v1",
            connected,
            extension_version: state.extension_version.clone(),
            last_seen_at: state.last_seen_at,
            active_job_count: state.pending.len(),
            queued_job_count: state.queue.len(),
            jobs,
        }
    }

    pub fn poll(&self, extension_version: Option<String>) -> Option<BrowserExtensionJob> {
        self.heartbeat(extension_version);
        let mut state = self.state.lock().ok()?;
        let job = state.queue.pop_front()?;
        if let Some(live_job) = state.live_jobs.get_mut(&job.browser_job_id) {
            live_job.view.status = "running".to_string();
            live_job.view.stage = "opening".to_string();
            live_job.view.updated_at = now_ms();
        }
        Some(job)
    }

    pub fn progress(&self, progress: BrowserExtensionProgress) -> bool {
        self.heartbeat(None);
        let Ok(mut state) = self.state.lock() else {
            return false;
        };
        let Some(job) = state.live_jobs.get_mut(&progress.browser_job_id) else {
            return false;
        };
        if !progress.status.trim().is_empty() {
            job.view.status = progress.status.trim().to_string();
        }
        if !progress.stage.trim().is_empty() {
            job.view.stage = progress.stage.trim().to_string();
        }
        if !progress.title.trim().is_empty() {
            job.view.title = progress.title.trim().chars().take(200).collect();
        }
        if !progress.url.trim().is_empty() {
            job.view.url = display_url(&progress.url);
        }
        if let Some(encoded) = progress.preview_base64.as_deref() {
            if let Ok(bytes) = BASE64_STANDARD.decode(encoded) {
                if !bytes.is_empty() && bytes.len() <= MAX_PREVIEW_BYTES {
                    let mime_type = progress
                        .preview_mime_type
                        .as_deref()
                        .unwrap_or("image/jpeg");
                    if matches!(mime_type, "image/jpeg" | "image/png") {
                        job.preview_bytes = Some(bytes);
                        job.preview_mime_type = Some(mime_type.to_string());
                        job.view.has_preview = true;
                        job.view.preview_revision = job.view.preview_revision.saturating_add(1);
                    }
                }
            }
        }
        job.view.updated_at = now_ms();
        true
    }

    pub fn preview(&self, job_id: &str) -> Option<(String, Vec<u8>)> {
        let state = self.state.lock().ok()?;
        let job = state.live_jobs.get(job_id)?;
        Some((job.preview_mime_type.clone()?, job.preview_bytes.clone()?))
    }

    pub fn complete(&self, result: BrowserExtensionResult) -> bool {
        self.heartbeat(None);
        let sender = self.state.lock().ok().and_then(|mut state| {
            if let Some(job) = state.live_jobs.get_mut(&result.browser_job_id) {
                job.view.status = if matches!(result.status.as_str(), "complete" | "partial") {
                    "completed".to_string()
                } else {
                    "failed".to_string()
                };
                job.view.stage = if job.view.status == "completed" {
                    "complete".to_string()
                } else {
                    "failed".to_string()
                };
                if !result.title.trim().is_empty() {
                    job.view.title = result.title.trim().chars().take(200).collect();
                }
                if !result.url.trim().is_empty() {
                    job.view.url = display_url(&result.url);
                }
                job.view.updated_at = now_ms();
            }
            state.pending.remove(&result.browser_job_id)
        });
        sender.is_some_and(|sender| sender.send(result).is_ok())
    }

    pub fn handle_bridge_message(&self, message: BrowserBridgeMessage) -> BrowserBridgeResponse {
        match message.message_type.as_str() {
            "heartbeat" => {
                self.heartbeat(message.extension_version);
                BrowserBridgeResponse {
                    ok: true,
                    job: None,
                    error: None,
                }
            }
            "poll" => BrowserBridgeResponse {
                ok: true,
                job: self.poll(message.extension_version),
                error: None,
            },
            "result" => match message.result {
                Some(result) => BrowserBridgeResponse {
                    ok: self.complete(result),
                    job: None,
                    error: None,
                },
                None => BrowserBridgeResponse {
                    ok: false,
                    job: None,
                    error: Some("RESULT_REQUIRED"),
                },
            },
            "progress" => match message.progress {
                Some(progress) => BrowserBridgeResponse {
                    ok: self.progress(progress),
                    job: None,
                    error: None,
                },
                None => BrowserBridgeResponse {
                    ok: false,
                    job: None,
                    error: Some("PROGRESS_REQUIRED"),
                },
            },
            _ => BrowserBridgeResponse {
                ok: false,
                job: None,
                error: Some("MESSAGE_TYPE_UNSUPPORTED"),
            },
        }
    }

    pub async fn submit(
        &self,
        job: BrowserExtensionJob,
        timeout: Duration,
    ) -> Result<BrowserExtensionResult, BrowserExtensionError> {
        if !self.status().connected {
            return Err(BrowserExtensionError::Unavailable);
        }
        let job_id = job.browser_job_id.clone();
        let (sender, receiver) = oneshot::channel();
        {
            let mut state = self
                .state
                .lock()
                .map_err(|_| BrowserExtensionError::Internal)?;
            state.pending.insert(job_id.clone(), sender);
            state.live_jobs.insert(
                job_id.clone(),
                BrowserLiveJob {
                    view: BrowserLiveJobView {
                        browser_job_id: job_id.clone(),
                        url: display_url(&job.url),
                        title: String::new(),
                        status: "queued".to_string(),
                        stage: "queued".to_string(),
                        updated_at: now_ms(),
                        has_preview: false,
                        preview_revision: 0,
                    },
                    preview_mime_type: None,
                    preview_bytes: None,
                },
            );
            state.queue.push_back(job);
        }
        let result = tokio::time::timeout(timeout, receiver).await;
        let mut state = self
            .state
            .lock()
            .map_err(|_| BrowserExtensionError::Internal)?;
        state.queue.retain(|queued| queued.browser_job_id != job_id);
        state.pending.remove(&job_id);
        if matches!(result, Err(_) | Ok(Err(_))) {
            if let Some(job) = state.live_jobs.get_mut(&job_id) {
                job.view.status = "failed".to_string();
                job.view.stage = if result.is_err() {
                    "timeout".to_string()
                } else {
                    "disconnected".to_string()
                };
                job.view.updated_at = now_ms();
            }
        }
        drop(state);
        match result {
            Ok(Ok(result)) if matches!(result.status.as_str(), "complete" | "partial") => {
                Ok(result)
            }
            Ok(Ok(result)) => Err(BrowserExtensionError::Failed(
                result
                    .error_code
                    .unwrap_or_else(|| "EXTENSION_SCRAPE_FAILED".to_string()),
                result
                    .error_message
                    .unwrap_or_else(|| "Chrome 扩展未能读取页面".to_string()),
            )),
            Ok(Err(_)) => Err(BrowserExtensionError::Internal),
            Err(_) => Err(BrowserExtensionError::Timeout),
        }
    }
}

pub fn bounded_browser_bridge_socket_path(path: PathBuf) -> PathBuf {
    if path.to_string_lossy().as_bytes().len() <= BROWSER_BRIDGE_SOCKET_MAX_BYTES {
        return path;
    }

    // macOS 的 sockaddr_un.sun_path 只有 104 字节。打包版会把 HOME 指向
    // `~/Library/Application Support/.../runtime`，直接拼接 socket 文件名会让
    // Core 在监听 REST API 前退出。对超长路径使用稳定哈希生成短路径；Native
    // Messaging Host 复用同一函数，因此服务端与连接端仍会命中同一个 socket。
    let mut hasher = DefaultHasher::new();
    path.hash(&mut hasher);
    PathBuf::from("/tmp").join(format!(
        "memorybread-browser-bridge-{:016x}.sock",
        hasher.finish()
    ))
}

pub fn browser_bridge_socket_path() -> PathBuf {
    if let Some(path) = std::env::var_os(BROWSER_BRIDGE_SOCKET_ENV) {
        return bounded_browser_bridge_socket_path(PathBuf::from(path));
    }
    let home = std::env::var_os("HOME").unwrap_or_else(|| ".".into());
    bounded_browser_bridge_socket_path(
        PathBuf::from(home)
            .join(".memory-bread")
            .join("browser-bridge.sock"),
    )
}

#[cfg(unix)]
pub fn start_browser_bridge_server(broker: BrowserExtensionBroker) -> std::io::Result<PathBuf> {
    use std::os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
    };

    let socket_path = browser_bridge_socket_path();
    let parent = socket_path.parent().ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "bridge socket parent missing",
        )
    })?;
    fs::create_dir_all(parent)?;
    if socket_path.exists() {
        if UnixStream::connect(&socket_path).is_ok() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::AddrInUse,
                "browser bridge socket is already active",
            ));
        }
        fs::remove_file(&socket_path)?;
    }
    let listener = UnixListener::bind(&socket_path)?;
    fs::set_permissions(&socket_path, fs::Permissions::from_mode(0o600))?;
    let reported_path = socket_path.clone();
    thread::Builder::new()
        .name("memorybread-browser-bridge".to_string())
        .spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { continue };
                let Ok(reader_stream) = stream.try_clone() else {
                    continue;
                };
                let mut reader = BufReader::new(reader_stream);
                let mut writer = BufWriter::new(stream);
                loop {
                    let mut bytes = Vec::new();
                    match reader.read_until(b'\n', &mut bytes) {
                        Ok(0) | Err(_) => break,
                        Ok(_) if bytes.len() > MAX_BRIDGE_MESSAGE_BYTES => break,
                        Ok(_) => {}
                    }
                    let response = match serde_json::from_slice::<BrowserBridgeMessage>(&bytes) {
                        Ok(message) => broker.handle_bridge_message(message),
                        Err(_) => BrowserBridgeResponse {
                            ok: false,
                            job: None,
                            error: Some("MESSAGE_INVALID"),
                        },
                    };
                    if serde_json::to_writer(&mut writer, &response).is_err()
                        || writer.write_all(b"\n").is_err()
                        || writer.flush().is_err()
                    {
                        break;
                    }
                }
            }
        })?;
    Ok(reported_path)
}

impl Default for BrowserExtensionBroker {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn browser_bridge_socket_keeps_short_paths_unchanged() {
        let path = PathBuf::from("/tmp/memorybread-browser-bridge.sock");
        assert_eq!(bounded_browser_bridge_socket_path(path.clone()), path);
    }

    #[test]
    fn browser_bridge_socket_shortens_packaged_runtime_paths_deterministically() {
        let path = PathBuf::from(format!(
            "/Users/test/Library/Application Support/{}/runtime/.memory-bread/browser-bridge.sock",
            "memorybread-client-with-a-long-sandbox-name".repeat(3)
        ));
        let shortened = bounded_browser_bridge_socket_path(path.clone());

        assert!(shortened.to_string_lossy().as_bytes().len() <= BROWSER_BRIDGE_SOCKET_MAX_BYTES);
        assert_eq!(shortened.parent(), Some(std::path::Path::new("/tmp")));
        assert_eq!(
            bounded_browser_bridge_socket_path(path),
            shortened,
            "the Core and Native Messaging Host must derive the same socket path"
        );
        assert!(shortened
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.starts_with("memorybread-browser-bridge-")));
    }

    #[tokio::test]
    async fn disconnected_broker_fails_without_queueing() {
        let broker = BrowserExtensionBroker::new();
        let job = BrowserExtensionJob::new(
            "https://example.com".to_string(),
            None,
            vec![],
            None,
            None,
            Duration::from_secs(1),
        );
        assert!(matches!(
            broker.submit(job, Duration::from_millis(10)).await,
            Err(BrowserExtensionError::Unavailable)
        ));
        assert_eq!(broker.status().queued_job_count, 0);
    }

    #[tokio::test]
    async fn polled_result_resolves_matching_job() {
        let broker = BrowserExtensionBroker::new();
        broker.heartbeat(Some("0.1.0".to_string()));
        let job = BrowserExtensionJob::new(
            "https://example.com/report".to_string(),
            Some("读取本周数据".to_string()),
            vec!["订单量".to_string()],
            None,
            None,
            Duration::from_secs(1),
        );
        let expected_id = job.browser_job_id.clone();
        let submitter = {
            let broker = broker.clone();
            tokio::spawn(async move { broker.submit(job, Duration::from_secs(1)).await })
        };
        tokio::task::yield_now().await;
        let polled = broker.poll(Some("0.1.0".to_string())).expect("job queued");
        assert_eq!(polled.browser_job_id, expected_id);
        assert!(broker.complete(BrowserExtensionResult {
            browser_job_id: expected_id,
            status: "complete".to_string(),
            title: "报表".to_string(),
            url: "https://example.com/report".to_string(),
            content_text: "订单量 12".to_string(),
            structured_data: Value::Null,
            completeness: serde_json::json!({"status": "complete"}),
            error_code: None,
            error_message: None,
        }));
        assert!(submitter.await.expect("task joined").is_ok());
    }

    #[tokio::test]
    async fn live_progress_exposes_short_lived_preview_without_persisting_it() {
        let broker = BrowserExtensionBroker::new();
        broker.heartbeat(Some("0.2.0".to_string()));
        let job = BrowserExtensionJob::new(
            "https://example.com/dashboard".to_string(),
            Some("读取经营指标".to_string()),
            vec![],
            None,
            None,
            Duration::from_secs(1),
        );
        let expected_id = job.browser_job_id.clone();
        let submitter = {
            let broker = broker.clone();
            tokio::spawn(async move { broker.submit(job, Duration::from_secs(1)).await })
        };
        tokio::task::yield_now().await;
        broker.poll(Some("0.2.0".to_string())).expect("job queued");
        assert!(broker.progress(BrowserExtensionProgress {
            browser_job_id: expected_id.clone(),
            status: "running".to_string(),
            stage: "reading".to_string(),
            title: "经营看板".to_string(),
            url: "https://example.com/dashboard".to_string(),
            preview_base64: Some(BASE64_STANDARD.encode(b"jpeg-preview")),
            preview_mime_type: Some("image/jpeg".to_string()),
        }));

        let status = broker.status();
        assert_eq!(status.jobs[0].browser_job_id, expected_id);
        assert_eq!(status.jobs[0].stage, "reading");
        assert!(status.jobs[0].has_preview);
        assert_eq!(broker.preview(&expected_id).unwrap().1, b"jpeg-preview");

        assert!(broker.complete(BrowserExtensionResult {
            browser_job_id: expected_id,
            status: "complete".to_string(),
            title: "经营看板".to_string(),
            url: "https://example.com/dashboard".to_string(),
            content_text: "订单量 12".to_string(),
            structured_data: Value::Null,
            completeness: Value::Null,
            error_code: None,
            error_message: None,
        }));
        assert!(submitter.await.expect("task joined").is_ok());
        assert_eq!(broker.status().jobs[0].status, "completed");
    }
}
