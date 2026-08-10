//! 事件监听器 — 前台上下文变化触发 + 定时兜底采集
//!
//! 变化监听先轻量比较前台应用和浏览器 URL，仅在发生变化时触发完整采集；
//! 低频定时路径继续负责 90 秒兜底采集。

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::sync::Notify;
use tokio::time::interval;
use tracing::{debug, info, warn};

use super::{ax::get_frontmost_context_snapshot_async, ax::AXInfo, CaptureEvent};
use crate::monitor::SystemPressureState;
use crate::storage::{models::NewCaptureAttempt, StorageManager};

/// 采集定时器的运行时状态。设置页更新后通过 `Notify` 立即唤醒监听器，
/// `effective_interval_secs` 则供监控页展示压力退避后的真实间隔。
#[derive(Debug)]
pub struct CaptureSchedule {
    configured_interval_secs: AtomicU64,
    effective_interval_secs: AtomicU64,
    pressure_degraded: AtomicBool,
    changed: Notify,
}

impl CaptureSchedule {
    pub fn new(interval_secs: u64) -> Self {
        let interval_secs = interval_secs.max(1);
        Self {
            configured_interval_secs: AtomicU64::new(interval_secs),
            effective_interval_secs: AtomicU64::new(interval_secs),
            pressure_degraded: AtomicBool::new(false),
            changed: Notify::new(),
        }
    }

    pub fn configured_interval_secs(&self) -> u64 {
        self.configured_interval_secs.load(Ordering::Relaxed).max(1)
    }

    pub fn effective_interval_secs(&self) -> u64 {
        self.effective_interval_secs.load(Ordering::Relaxed).max(1)
    }

    pub fn pressure_degraded(&self) -> bool {
        self.pressure_degraded.load(Ordering::Relaxed)
    }

    pub fn update_configured_interval(&self, interval_secs: u64) {
        let interval_secs = interval_secs.max(1);
        self.configured_interval_secs
            .store(interval_secs, Ordering::Relaxed);
        self.effective_interval_secs
            .store(interval_secs, Ordering::Relaxed);
        self.pressure_degraded.store(false, Ordering::Relaxed);
        self.changed.notify_waiters();
    }

    fn update_effective_interval(&self, interval_secs: u64, pressure_degraded: bool) {
        self.effective_interval_secs
            .store(interval_secs.max(1), Ordering::Relaxed);
        self.pressure_degraded
            .store(pressure_degraded, Ordering::Relaxed);
    }
}

/// 内存压力等级
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MemoryPressure {
    Normal,   // < 70%
    High,     // 70-85%
    Critical, // > 85%
}

/// 事件监听器配置
#[derive(Clone)]
pub struct ListenerConfig {
    /// 定时采集间隔（秒）
    pub interval_secs: u64,
    /// 运行时开关（可被外部控制）
    pub enabled: Arc<AtomicBool>,
    /// 空闲阈值（秒），超过此时间无操作则暂停采集
    pub idle_threshold_secs: u64,
    /// ResourceMonitor 更新的系统/WindowServer 压力状态
    pub system_pressure: SystemPressureState,
    /// 设置页与监听器共享的动态间隔状态。
    pub schedule: Arc<CaptureSchedule>,
    /// 可选审计存储；测试可不依赖真实数据库。
    pub audit_storage: Option<StorageManager>,
}

impl Default for ListenerConfig {
    fn default() -> Self {
        Self {
            interval_secs: 90, // 默认 90 秒兜底采集
            enabled: Arc::new(AtomicBool::new(true)),
            idle_threshold_secs: 300, // 5 分钟无操作暂停
            system_pressure: SystemPressureState::default(),
            schedule: Arc::new(CaptureSchedule::new(90)),
            audit_storage: None,
        }
    }
}

/// 活跃状态下即使系统有压力，也不允许把兜底关键帧拉长到 2 分钟以上。
const MAX_BACKOFF_SECS: u64 = 120;
const LOW_MEMORY_THRESHOLD_MB: u64 = 500;
const CONTEXT_WATCH_INTERVAL_SECS: u64 = 5;

#[derive(Debug, Clone, PartialEq, Eq)]
struct ObservedContext {
    app_name: String,
    bundle_id: Option<String>,
    win_title: Option<String>,
    url: Option<String>,
    webpage_title: Option<String>,
}

impl ObservedContext {
    fn from_ax_info(info: AXInfo) -> Option<Self> {
        let app_name = info.app_name?.trim().to_string();
        if app_name.is_empty() {
            return None;
        }
        Some(Self {
            app_name,
            bundle_id: non_empty(info.app_bundle_id),
            win_title: non_empty(info.win_title),
            url: non_empty(info.url),
            webpage_title: non_empty(info.webpage_title),
        })
    }

    fn same_app(&self, other: &Self) -> bool {
        match (self.bundle_id.as_deref(), other.bundle_id.as_deref()) {
            (Some(left), Some(right)) => left == right,
            _ => self.app_name == other.app_name,
        }
    }

    fn retain_transient_browser_metadata(&mut self, previous: &Self) {
        if !self.same_app(previous) || self.url.is_some() || previous.url.is_none() {
            return;
        }
        self.url = previous.url.clone();
        if self.webpage_title.is_none() {
            self.webpage_title = previous.webpage_title.clone();
        }
        if self.win_title.is_none() {
            self.win_title = previous.win_title.clone();
        }
    }

    fn app_switch_event(&self) -> CaptureEvent {
        CaptureEvent::AppSwitch {
            app_name: self.app_name.clone(),
            bundle_id: self.bundle_id.clone(),
            win_title: self.win_title.clone().unwrap_or_default(),
        }
    }

    fn browser_navigation_event(&self) -> Option<CaptureEvent> {
        Some(CaptureEvent::BrowserNavigation {
            app_name: self.app_name.clone(),
            bundle_id: self.bundle_id.clone(),
            win_title: self.win_title.clone(),
            url: self.url.clone()?,
            webpage_title: self.webpage_title.clone(),
        })
    }
}

fn non_empty(value: Option<String>) -> Option<String> {
    value.and_then(|value| {
        let trimmed = value.trim();
        (!trimmed.is_empty()).then(|| trimmed.to_string())
    })
}

fn context_change_event(
    previous: Option<&ObservedContext>,
    current: &ObservedContext,
) -> Option<CaptureEvent> {
    let Some(previous) = previous else {
        return current
            .browser_navigation_event()
            .or_else(|| Some(current.app_switch_event()));
    };

    if !current.same_app(previous) {
        return current
            .browser_navigation_event()
            .or_else(|| Some(current.app_switch_event()));
    }

    match (previous.url.as_deref(), current.url.as_deref()) {
        (_, Some(current_url)) if previous.url.as_deref() != Some(current_url) => {
            current.browser_navigation_event()
        }
        _ => None,
    }
}

/// 启动前台上下文变化监听。
///
/// 每 5 秒读取应用身份和浏览器 URL；应用或 URL 变化时触发完整 AX 采集，
/// AX 正文为空时再由采集引擎截图并异步 OCR。
pub async fn start_context_watcher(
    enabled: Arc<AtomicBool>,
    system_pressure: SystemPressureState,
    audit_storage: Option<StorageManager>,
    tx: mpsc::Sender<CaptureEvent>,
) {
    info!(
        interval_secs = CONTEXT_WATCH_INTERVAL_SECS,
        "启动前台上下文变化监听器"
    );
    let mut ticker = interval(Duration::from_secs(CONTEXT_WATCH_INTERVAL_SECS));
    let mut previous: Option<ObservedContext> = None;

    loop {
        ticker.tick().await;

        if !enabled.load(Ordering::Relaxed) {
            previous = None;
            continue;
        }

        let Some(info) = get_frontmost_context_snapshot_async().await else {
            continue;
        };
        let Some(mut current) = ObservedContext::from_ax_info(info) else {
            continue;
        };
        if let Some(previous) = previous.as_ref() {
            current.retain_transient_browser_metadata(previous);
        }

        let Some(event) = context_change_event(previous.as_ref(), &current) else {
            previous = Some(current);
            continue;
        };

        let asleep = is_display_asleep_async().await;
        let avail_mb = get_available_memory_mb_async().await;
        let mem_blocked = avail_mb
            .map(|mb| mb < LOW_MEMORY_THRESHOLD_MB)
            .unwrap_or(false);
        let ax_tripped = super::ax::is_circuit_breaker_tripped();
        let load_pressure = system_pressure.snapshot();
        // 系统 CPU / WindowServer 压力不再关闭高价值的上下文变化事件；只有屏幕睡眠、
        // 极低可用内存和 AX 熔断才降级跳过，并留下持久审计。
        if asleep || mem_blocked || ax_tripped {
            let reason = if asleep {
                "display_asleep"
            } else if mem_blocked {
                "low_available_memory"
            } else {
                "ax_circuit_breaker"
            };
            record_listener_attempt(
                audit_storage.as_ref(),
                &event,
                "degraded",
                reason,
                Some(&current),
                Some(CONTEXT_WATCH_INTERVAL_SECS),
            );
            warn!(
                asleep,
                avail_mb = ?avail_mb,
                ax_tripped,
                system_cpu = load_pressure.system_cpu_percent,
                window_server_cpu = load_pressure.window_server_cpu_percent,
                "变化采集门禁命中，本轮不推进上下文，5 秒后重试"
            );
            continue;
        }

        let audit_event = event.clone();
        match tokio::time::timeout(Duration::from_secs(1), tx.send(event)).await {
            Ok(Ok(())) => previous = Some(current),
            Ok(Err(_)) => {
                info!("采集引擎已关闭，停止前台上下文变化监听器");
                break;
            }
            Err(_) => {
                record_listener_attempt(
                    audit_storage.as_ref(),
                    &audit_event,
                    "failed",
                    "event_queue_timeout",
                    Some(&current),
                    Some(CONTEXT_WATCH_INTERVAL_SECS),
                );
                warn!("发送前台上下文变化事件超时，下一轮重试");
            }
        }
    }
}

/// 启动事件监听器（自适应采集策略）
///
/// 三联门禁（任一命中 → 跳过本次 + 下次 tick 间隔翻倍至 120s 上限）：
/// 1. 显示器睡眠（ioreg IODisplayWrangler.CurrentPowerState < 4）
/// 2. 可用内存 < 500MB
/// 3. AX 调用熔断中（连续超时 5 次后的 30s 冷却期）
///
/// 未命中且当前 interval > base 时，按 (current + base) / 2 收敛回 base。
pub async fn start_listener(config: ListenerConfig, tx: mpsc::Sender<CaptureEvent>) {
    let configured_interval = config.schedule.configured_interval_secs();
    info!(
        "启动自适应事件监听器，基础间隔 {}s，回退上限 {}s",
        configured_interval, MAX_BACKOFF_SECS
    );

    let mut base_interval = configured_interval;
    let mut current_interval = base_interval;
    config
        .schedule
        .update_effective_interval(current_interval, false);

    loop {
        tokio::select! {
            _ = tokio::time::sleep(Duration::from_secs(current_interval)) => {}
            _ = config.schedule.changed.notified() => {
                base_interval = config.schedule.configured_interval_secs();
                current_interval = base_interval;
                config.schedule.update_effective_interval(current_interval, false);
                info!(interval_secs = current_interval, "采集间隔已实时更新");
                continue;
            }
        }

        if !config.enabled.load(Ordering::Relaxed) {
            debug!("采集已暂停，等待 5 秒后重试");
            tokio::time::sleep(Duration::from_secs(5)).await;
            continue;
        }

        // ── 三联门禁 ──────────────────────────────────────────────────────────
        let asleep = is_display_asleep_async().await;
        let avail_mb = get_available_memory_mb_async().await;
        let mem_blocked = avail_mb
            .map(|mb| mb < LOW_MEMORY_THRESHOLD_MB)
            .unwrap_or(false);
        let ax_tripped = super::ax::is_circuit_breaker_tripped();
        let load_pressure = config.system_pressure.snapshot();

        if asleep || mem_blocked || ax_tripped || load_pressure.under_pressure {
            if load_pressure.under_pressure && !asleep && !mem_blocked && !ax_tripped {
                // 活跃但高负载：不丢本轮采集，只延长下一轮间隔；OCR 本身在后台执行。
            } else {
                let new_interval = current_interval
                    .saturating_mul(2)
                    .min(MAX_BACKOFF_SECS)
                    .max(base_interval);
                let reason = if asleep {
                    "display_asleep"
                } else if mem_blocked {
                    "low_available_memory"
                } else {
                    "ax_circuit_breaker"
                };
                record_listener_attempt(
                    config.audit_storage.as_ref(),
                    &CaptureEvent::Periodic,
                    "degraded",
                    reason,
                    None,
                    Some(new_interval),
                );
                warn!(
                    asleep,
                    avail_mb = ?avail_mb,
                    ax_tripped,
                    system_cpu = load_pressure.system_cpu_percent,
                    window_server_cpu = load_pressure.window_server_cpu_percent,
                    from = current_interval,
                    to = new_interval,
                    "门禁命中，跳过本次采集并延长 tick"
                );
                if new_interval != current_interval {
                    current_interval = new_interval;
                }
                config
                    .schedule
                    .update_effective_interval(current_interval, true);
                continue;
            }
        }

        // ── 决定下一次 tick 间隔 ──────────────────────────────────────────────
        // 门禁未命中时把 ticker 拉回去；同时考虑内存压力等级的 target。
        let pressure = get_memory_pressure_async().await;
        let pressure_target = match pressure {
            MemoryPressure::Normal => base_interval,
            MemoryPressure::High => (base_interval.saturating_mul(3)).min(MAX_BACKOFF_SECS),
            MemoryPressure::Critical => (base_interval.saturating_mul(5)).min(MAX_BACKOFF_SECS),
        };

        // 收敛目标：取压力 target 与 (current+base)/2 的较大者，避免在压力下被收敛压回 base。
        let converged = ((current_interval + base_interval) / 2).max(base_interval);
        let next_interval = pressure_target.max(converged).min(MAX_BACKOFF_SECS);

        if next_interval != current_interval {
            info!(
                pressure = ?pressure,
                from = current_interval,
                to = next_interval,
                "调整 tick 间隔"
            );
            current_interval = next_interval;
        }
        config.schedule.update_effective_interval(
            current_interval,
            load_pressure.under_pressure || pressure != MemoryPressure::Normal,
        );

        // 系统空闲时间检测
        if let Ok(idle_secs) = get_system_idle_time_secs().await {
            if idle_secs > config.idle_threshold_secs {
                record_listener_attempt(
                    config.audit_storage.as_ref(),
                    &CaptureEvent::Periodic,
                    "degraded",
                    "system_idle",
                    None,
                    Some(current_interval),
                );
                debug!("系统空闲 {} 秒，跳过本次采集", idle_secs);
                continue;
            }
        }

        debug!(
            "触发定时采集事件 (内存压力: {:?}, interval: {}s)",
            pressure, current_interval
        );

        match tokio::time::timeout(Duration::from_secs(5), tx.send(CaptureEvent::Periodic)).await {
            Ok(Ok(_)) => {}
            Ok(Err(_)) => {
                info!("采集引擎已关闭，停止监听器");
                break;
            }
            Err(_) => {
                record_listener_attempt(
                    config.audit_storage.as_ref(),
                    &CaptureEvent::Periodic,
                    "failed",
                    "event_queue_timeout",
                    None,
                    Some(current_interval),
                );
                warn!("发送采集事件超时（5 秒），跳过本次");
            }
        }
    }
}

fn record_listener_attempt(
    storage: Option<&StorageManager>,
    event: &CaptureEvent,
    outcome: &str,
    reason: &str,
    context: Option<&ObservedContext>,
    effective_interval_secs: Option<u64>,
) {
    let Some(storage) = storage else {
        return;
    };
    let attempt = NewCaptureAttempt {
        observed_at: crate::storage::db::current_ts_ms(),
        event_type: event.to_event_type().as_str().to_string(),
        outcome: outcome.to_string(),
        reason: reason.to_string(),
        capture_id: None,
        related_capture_id: None,
        app_name: context.map(|value| value.app_name.clone()),
        win_title: context.and_then(|value| value.win_title.clone()),
        is_private: false,
        effective_interval_secs,
    };
    if let Err(error) = storage.insert_capture_attempt(&attempt) {
        warn!(%error, reason, "记录采集降级审计失败");
    }
}

/// 获取系统内存压力等级（异步版本）
async fn get_memory_pressure_async() -> MemoryPressure {
    tokio::task::spawn_blocking(get_memory_pressure)
        .await
        .unwrap_or(MemoryPressure::Normal)
}

/// 获取系统空闲时间（异步版本）
pub(crate) async fn get_system_idle_time_secs() -> Result<u64, ()> {
    tokio::task::spawn_blocking(get_system_idle_time)
        .await
        .map_err(|_| ())?
}

/// 检测显示器是否睡眠（异步版本，1 秒超时）
async fn is_display_asleep_async() -> bool {
    match tokio::time::timeout(
        Duration::from_secs(1),
        tokio::task::spawn_blocking(is_display_asleep),
    )
    .await
    {
        Ok(Ok(v)) => v,
        _ => false, // 超时或 panic 时 fail-open
    }
}

/// 获取可用内存（异步版本，1 秒超时）
async fn get_available_memory_mb_async() -> Option<u64> {
    match tokio::time::timeout(
        Duration::from_secs(1),
        tokio::task::spawn_blocking(get_available_memory_mb),
    )
    .await
    {
        Ok(Ok(Ok(mb))) => Some(mb),
        _ => None,
    }
}

/// 获取系统内存压力等级
#[cfg(target_os = "macos")]
fn get_memory_pressure() -> MemoryPressure {
    use std::process::Command;

    if let Some(pressure) = get_memory_pressure_from_memory_pressure() {
        return pressure;
    }

    let output = match Command::new("vm_stat").output() {
        Ok(o) => o,
        Err(_) => return MemoryPressure::Normal,
    };

    let stdout = String::from_utf8_lossy(&output.stdout);

    // 解析 vm_stat 输出
    let mut pages_free = 0u64;
    let mut pages_active = 0u64;
    let mut pages_inactive = 0u64;
    let mut pages_wired = 0u64;
    let mut pages_compressed = 0u64; // 压缩内存
    let mut pages_speculative = 0u64;

    for line in stdout.lines() {
        if line.starts_with("Pages free:") {
            pages_free = parse_vm_stat_value(line);
        } else if line.starts_with("Pages active:") {
            pages_active = parse_vm_stat_value(line);
        } else if line.starts_with("Pages inactive:") {
            pages_inactive = parse_vm_stat_value(line);
        } else if line.starts_with("Pages speculative:") {
            pages_speculative = parse_vm_stat_value(line);
        } else if line.starts_with("Pages wired down:") {
            pages_wired = parse_vm_stat_value(line);
        } else if line.starts_with("Pages occupied by compressor:") {
            // compressor 实际物理占用（已压缩后的 RAM）。
            // 不要用 "Pages stored in compressor:"，那是压缩前的逻辑页数，
            // 在重压力下会高于物理总页数，导致 usage 溢出 100%。
            pages_compressed = parse_vm_stat_value(line);
        }
    }

    // vm_stat 不直接给出物理总页数；compressor 会占用真实物理页，必须进入分母，
    // 否则 used_pages 可能大于 total_pages，导致长期误判为 Critical。
    let total_pages = pages_free
        + pages_speculative
        + pages_active
        + pages_inactive
        + pages_wired
        + pages_compressed;
    if total_pages == 0 {
        return MemoryPressure::Normal;
    }

    let used_pages = pages_active + pages_wired + pages_compressed;
    let usage_percent = (used_pages * 100) / total_pages;

    match usage_percent {
        0..=69 => MemoryPressure::Normal, // < 70%
        70..=84 => MemoryPressure::High,  // 70-85%
        _ => MemoryPressure::Critical,    // >= 85%
    }
}

#[cfg(target_os = "macos")]
fn get_memory_pressure_from_memory_pressure() -> Option<MemoryPressure> {
    use std::process::Command;

    let output = Command::new("memory_pressure").output().ok()?;
    if !output.status.success() {
        return None;
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let free_percent = stdout.lines().find_map(|line| {
        let prefix = "System-wide memory free percentage:";
        let value = line
            .trim()
            .strip_prefix(prefix)?
            .trim()
            .trim_end_matches('%');
        value.parse::<u64>().ok()
    })?;

    match free_percent {
        0..=14 => Some(MemoryPressure::Critical),
        15..=29 => Some(MemoryPressure::High),
        _ => Some(MemoryPressure::Normal),
    }
}

#[cfg(target_os = "macos")]
fn parse_vm_stat_value(line: &str) -> u64 {
    line.split(':')
        .nth(1)
        .and_then(|s| s.trim().trim_end_matches('.').parse().ok())
        .unwrap_or(0)
}

#[cfg(not(target_os = "macos"))]
fn get_memory_pressure() -> MemoryPressure {
    MemoryPressure::Normal
}

/// 获取系统空闲时间（秒）
///
/// macOS: 使用 ioreg 命令（低频调用安全）
/// 其他平台: 返回 Err
#[cfg(target_os = "macos")]
fn get_system_idle_time() -> Result<u64, ()> {
    use std::process::Command;

    // 使用 ioreg 查询 HIDIdleTime（单位：纳秒）
    // 注意：此方法仅在低频调用（>= 60 秒间隔）时安全
    let output = Command::new("ioreg")
        .args(["-c", "IOHIDSystem", "-d", "1"])
        .output()
        .map_err(|_| ())?;

    let stdout = String::from_utf8_lossy(&output.stdout);

    for line in stdout.lines() {
        if line.contains("HIDIdleTime") {
            // 格式: "HIDIdleTime" = 12345678901234
            if let Some(value_str) = line.split('=').nth(1) {
                let value_str = value_str.trim();
                if let Ok(idle_ns) = value_str.parse::<u64>() {
                    return Ok(idle_ns / 1_000_000_000); // 纳秒转秒
                }
            }
        }
    }

    Err(())
}

#[cfg(not(target_os = "macos"))]
fn get_system_idle_time() -> Result<u64, ()> {
    Err(())
}

/// 获取可用内存（MB）
///
/// macOS 上「可用」= free + inactive。inactive 是可被回收且未压缩的页，
/// 内核在压力下会优先复用它们，所以从用户视角应计入"可用"。
#[cfg(target_os = "macos")]
fn get_available_memory_mb() -> Result<u64, ()> {
    use std::process::Command;

    let output = Command::new("vm_stat").output().map_err(|_| ())?;
    let stdout = String::from_utf8_lossy(&output.stdout);

    let mut pages_free = 0u64;
    let mut pages_inactive = 0u64;
    let mut page_size = 4096u64;

    for line in stdout.lines() {
        if line.contains("page size of") {
            if let Some(size_str) = line.split_whitespace().last() {
                page_size = size_str.parse().unwrap_or(4096);
            }
        } else if line.starts_with("Pages free:") {
            pages_free = parse_vm_stat_value(line);
        } else if line.starts_with("Pages inactive:") {
            pages_inactive = parse_vm_stat_value(line);
        }
    }

    Ok(((pages_free + pages_inactive) * page_size) / (1024 * 1024))
}

#[cfg(not(target_os = "macos"))]
fn get_available_memory_mb() -> Result<u64, ()> {
    Err(())
}

/// 检测显示器是否处于睡眠/关闭状态。
///
/// macOS：通过 ioreg 查询 IODisplayWrangler 的 IOPowerManagement.CurrentPowerState：
/// - 4 = 亮屏；< 4 = 已变暗或关闭
///
/// 失败时返回 false（fail-open，避免误屏蔽采集）。
#[cfg(target_os = "macos")]
fn is_display_asleep() -> bool {
    use std::process::Command;

    let output = match Command::new("ioreg")
        .args([
            "-r",
            "-k",
            "IOPowerManagement",
            "-n",
            "IODisplayWrangler",
            "-d",
            "1",
        ])
        .output()
    {
        Ok(o) => o,
        Err(_) => return false,
    };

    let stdout = String::from_utf8_lossy(&output.stdout);
    for line in stdout.lines() {
        // 形如:  | "IOPowerManagement" = {"CurrentPowerState"=4,...}
        if let Some(idx) = line.find("CurrentPowerState") {
            let tail = &line[idx..];
            if let Some(eq_idx) = tail.find('=') {
                let after = &tail[eq_idx + 1..];
                let num: String = after.chars().take_while(|c| c.is_ascii_digit()).collect();
                if let Ok(state) = num.parse::<u32>() {
                    return state < 4;
                }
            }
        }
    }
    false
}

#[cfg(not(target_os = "macos"))]
fn is_display_asleep() -> bool {
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capture_intervals_keep_five_second_watch_and_ninety_second_fallback() {
        assert_eq!(CONTEXT_WATCH_INTERVAL_SECS, 5);
        assert_eq!(ListenerConfig::default().interval_secs, 90);
        assert_eq!(
            ListenerConfig::default()
                .schedule
                .configured_interval_secs(),
            90
        );
    }

    #[test]
    fn capture_schedule_updates_runtime_interval_and_clears_pressure_state() {
        let schedule = CaptureSchedule::new(90);
        schedule.update_effective_interval(120, true);
        assert_eq!(schedule.effective_interval_secs(), 120);
        assert!(schedule.pressure_degraded());

        schedule.update_configured_interval(30);
        assert_eq!(schedule.configured_interval_secs(), 30);
        assert_eq!(schedule.effective_interval_secs(), 30);
        assert!(!schedule.pressure_degraded());
    }

    fn context(app: &str, bundle_id: &str, url: Option<&str>) -> ObservedContext {
        ObservedContext {
            app_name: app.to_string(),
            bundle_id: Some(bundle_id.to_string()),
            win_title: Some("页面标题".to_string()),
            url: url.map(ToString::to_string),
            webpage_title: Some("页面标题".to_string()),
        }
    }

    #[test]
    fn initial_browser_context_emits_navigation() {
        let current = context(
            "Google Chrome",
            "com.google.Chrome",
            Some("https://example.com/a"),
        );
        let event = context_change_event(None, &current).unwrap();
        assert!(matches!(event, CaptureEvent::BrowserNavigation { .. }));
    }

    #[test]
    fn browser_url_change_emits_navigation_once() {
        let previous = context(
            "Google Chrome",
            "com.google.Chrome",
            Some("https://example.com/a"),
        );
        let current = context(
            "Google Chrome",
            "com.google.Chrome",
            Some("https://example.com/b"),
        );
        assert!(matches!(
            context_change_event(Some(&previous), &current),
            Some(CaptureEvent::BrowserNavigation { .. })
        ));
        assert!(context_change_event(Some(&current), &current).is_none());
    }

    #[test]
    fn app_change_without_url_emits_app_switch() {
        let previous = context(
            "Google Chrome",
            "com.google.Chrome",
            Some("https://example.com/a"),
        );
        let current = context("访达", "com.apple.finder", None);
        assert!(matches!(
            context_change_event(Some(&previous), &current),
            Some(CaptureEvent::AppSwitch { .. })
        ));
    }

    #[test]
    fn transient_browser_metadata_failure_keeps_previous_url() {
        let previous = context(
            "Google Chrome",
            "com.google.Chrome",
            Some("https://example.com/a"),
        );
        let mut current = context("Google Chrome", "com.google.Chrome", None);
        current.retain_transient_browser_metadata(&previous);
        assert_eq!(current.url, previous.url);
        assert!(context_change_event(Some(&previous), &current).is_none());
    }
}
