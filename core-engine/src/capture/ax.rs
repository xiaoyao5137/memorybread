//! Accessibility Tree 信息抓取
//!
//! macOS：通过 `osascript` 调用 System Events 获取前台应用名 / 窗口标题。
//! 未来可升级为直接调用 AXUIElement API（需要 Accessibility 权限）。
//!
//! 其他平台：返回 None，由调用方降级到 OCR。

use std::collections::HashMap;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Instant;

const EXTRACTED_TEXT_MAX_CHARS: usize = 500_000;
const GENERIC_FOCUS_MIN_CHARS: usize = 24;
const GENERIC_WINDOW_MIN_CHARS: usize = 48;
const GENERIC_STATIC_ITEM_LIMIT: usize = 80;
const GENERIC_ALL_UI_ITEM_LIMIT: usize = 140;

// 熔断机制：连续超时计数器
static TIMEOUT_COUNTER: AtomicU32 = AtomicU32::new(0);
static LAST_RESET_TIME: OnceLock<std::sync::Mutex<std::time::Instant>> = OnceLock::new();
const MAX_CONSECUTIVE_TIMEOUTS: u32 = 5;
const CIRCUIT_BREAKER_COOLDOWN_SECS: u64 = 30;
const AX_CACHE_TTL_SECS: u64 = 3600; // AX 支持缓存 1 小时

// AX 支持缓存：记录哪些应用不支持 AX，避免重复检测
static AX_SUPPORT_CACHE: OnceLock<Mutex<HashMap<String, (bool, Instant)>>> = OnceLock::new();

/// 从指定应用的指定后台窗口读取原生 Accessibility Tree 文本。
///
/// 与前台采集不同，这个入口不依赖 `front window`，用于创作过程中的专用
/// 浏览器窗口。AX 不可用、窗口不匹配或超时时返回 None，调用方再降级 DOM。
pub fn extract_window_text_by_title(
    app_process_name: &str,
    title_contains: &str,
) -> Option<String> {
    #[cfg(target_os = "macos")]
    {
        targeted_macos_ax::extract_window_text(app_process_name, title_contains)
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = (app_process_name, title_contains);
        None
    }
}

#[cfg(target_os = "macos")]
mod targeted_macos_ax {
    use super::EXTRACTED_TEXT_MAX_CHARS;
    use core_foundation::{
        array::CFArray,
        base::{CFRelease, CFType, CFTypeRef, TCFType},
        boolean::CFBoolean,
        string::{CFString, CFStringRef},
    };
    use std::{
        collections::HashSet,
        ffi::c_void,
        process::Command,
        ptr,
        time::{Duration, Instant},
    };

    type AXUIElementRef = *mut c_void;
    type AXValueRef = *const c_void;
    type AXError = i32;
    const AX_ERROR_SUCCESS: AXError = 0;
    const MAX_AX_NODES: usize = 4_000;
    const AX_TRAVERSAL_TIMEOUT: Duration = Duration::from_secs(4);
    const AX_VALUE_CGPOINT: i32 = 1;
    const AX_VALUE_CGSIZE: i32 = 2;

    #[repr(C)]
    #[derive(Default)]
    struct CGPoint {
        x: f64,
        y: f64,
    }

    #[repr(C)]
    #[derive(Default)]
    struct CGSize {
        width: f64,
        height: f64,
    }

    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXUIElementCreateApplication(pid: i32) -> AXUIElementRef;
        fn AXUIElementSetMessagingTimeout(element: AXUIElementRef, timeout: f32) -> AXError;
        fn AXUIElementCopyAttributeValue(
            element: AXUIElementRef,
            attribute: CFStringRef,
            value: *mut CFTypeRef,
        ) -> AXError;
        fn AXUIElementSetAttributeValue(
            element: AXUIElementRef,
            attribute: CFStringRef,
            value: CFTypeRef,
        ) -> AXError;
        fn AXValueGetValue(value: AXValueRef, value_type: i32, value_ptr: *mut c_void) -> bool;
    }

    fn process_id(process_name: &str) -> Option<i32> {
        let output = Command::new("pgrep")
            .args(["-x", process_name])
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        String::from_utf8_lossy(&output.stdout)
            .lines()
            .find_map(|line| line.trim().parse::<i32>().ok())
    }

    unsafe fn copy_attribute(element: AXUIElementRef, name: &str) -> Option<CFType> {
        if element.is_null() {
            return None;
        }
        let key = CFString::new(name);
        let mut value: CFTypeRef = ptr::null();
        let status = AXUIElementCopyAttributeValue(element, key.as_concrete_TypeRef(), &mut value);
        if status != AX_ERROR_SUCCESS || value.is_null() {
            return None;
        }
        Some(CFType::wrap_under_create_rule(value))
    }

    unsafe fn string_attribute(element: AXUIElementRef, name: &str) -> Option<String> {
        copy_attribute(element, name)?
            .downcast::<CFString>()
            .map(|value| value.to_string())
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
    }

    unsafe fn window_matches_dedicated_bounds(element: AXUIElementRef) -> bool {
        let Some(position_value) = copy_attribute(element, "AXPosition") else {
            return false;
        };
        let Some(size_value) = copy_attribute(element, "AXSize") else {
            return false;
        };
        let mut position = CGPoint::default();
        let mut size = CGSize::default();
        if !AXValueGetValue(
            position_value.as_CFTypeRef() as AXValueRef,
            AX_VALUE_CGPOINT,
            &mut position as *mut CGPoint as *mut c_void,
        ) || !AXValueGetValue(
            size_value.as_CFTypeRef() as AXValueRef,
            AX_VALUE_CGSIZE,
            &mut size as *mut CGSize as *mut c_void,
        ) {
            return false;
        }
        (position.x - 80.0).abs() <= 5.0
            && (position.y - 80.0).abs() <= 45.0
            && (size.width - 1_200.0).abs() <= 10.0
            && (size.height - 740.0).abs() <= 45.0
    }

    fn push_line(output: &mut String, seen: &mut HashSet<String>, raw: String) {
        for line in raw.lines() {
            let normalized = line.split_whitespace().collect::<Vec<_>>().join(" ");
            if normalized.is_empty() || !seen.insert(normalized.clone()) {
                continue;
            }
            if output.chars().count() >= EXTRACTED_TEXT_MAX_CHARS {
                return;
            }
            if !output.is_empty() {
                output.push('\n');
            }
            output.extend(
                normalized
                    .chars()
                    .take(EXTRACTED_TEXT_MAX_CHARS.saturating_sub(output.chars().count())),
            );
        }
    }

    unsafe fn visit_element(
        element: AXUIElementRef,
        started: Instant,
        visited: &mut HashSet<usize>,
        seen_lines: &mut HashSet<String>,
        output: &mut String,
    ) {
        if element.is_null()
            || started.elapsed() >= AX_TRAVERSAL_TIMEOUT
            || visited.len() >= MAX_AX_NODES
            || output.chars().count() >= EXTRACTED_TEXT_MAX_CHARS
            || !visited.insert(element as usize)
        {
            return;
        }

        // 把单次跨进程消息限制在较短时间内，避免某个异常 AX 节点阻塞采集。
        let _ = AXUIElementSetMessagingTimeout(element, 0.25);
        for attribute in ["AXValue", "AXTitle", "AXDescription", "AXHelp"] {
            if let Some(value) = string_attribute(element, attribute) {
                push_line(output, seen_lines, value);
            }
        }

        let Some(children) =
            copy_attribute(element, "AXChildren").and_then(|value| value.downcast::<CFArray>())
        else {
            return;
        };
        // 子元素指针只在 CFArray 存活时使用；递归不能延后到数组释放之后。
        for raw in children.get_all_values() {
            visit_element(raw as AXUIElementRef, started, visited, seen_lines, output);
            if started.elapsed() >= AX_TRAVERSAL_TIMEOUT
                || visited.len() >= MAX_AX_NODES
                || output.chars().count() >= EXTRACTED_TEXT_MAX_CHARS
            {
                break;
            }
        }
    }

    pub fn extract_window_text(process_name: &str, title_contains: &str) -> Option<String> {
        let pid = process_id(process_name)?;
        let title_contains = title_contains.trim();
        if title_contains.is_empty() {
            return None;
        }
        unsafe {
            let application = AXUIElementCreateApplication(pid);
            if application.is_null() {
                return None;
            }
            let _ = AXUIElementSetMessagingTimeout(application, 0.6);
            // Chromium/Electron 可能在没有屏幕阅读器时延迟构造渲染器 AX 树；
            // 通过公开 AX 属性显式开启后再读取，仍不触碰页面脚本或用户凭据。
            let enabled = CFBoolean::true_value();
            for attribute in ["AXEnhancedUserInterface", "AXManualAccessibility"] {
                let key = CFString::new(attribute);
                let _ = AXUIElementSetAttributeValue(
                    application,
                    key.as_concrete_TypeRef(),
                    enabled.as_CFTypeRef(),
                );
            }
            let result = (|| {
                let windows = copy_attribute(application, "AXWindows")?.downcast::<CFArray>()?;
                let window_values = windows.get_all_values();
                let target = window_values
                    .iter()
                    .copied()
                    .find(|raw| {
                        string_attribute(*raw as AXUIElementRef, "AXTitle")
                            .map(|title| title.contains(title_contains))
                            .unwrap_or(false)
                    })
                    .or_else(|| {
                        window_values
                            .iter()
                            .copied()
                            .find(|raw| window_matches_dedicated_bounds(*raw as AXUIElementRef))
                    })?;
                let mut visited = HashSet::new();
                let mut seen_lines = HashSet::new();
                let mut output = String::new();
                visit_element(
                    target as AXUIElementRef,
                    Instant::now(),
                    &mut visited,
                    &mut seen_lines,
                    &mut output,
                );
                let meaningful = output.chars().filter(|ch| ch.is_alphanumeric()).count();
                (meaningful >= 12).then_some(output)
            })();
            CFRelease(application as CFTypeRef);
            result
        }
    }
}

/// 从 Accessibility Tree 抓取到的前台应用信息
#[derive(Debug, Clone, Default)]
pub struct AXInfo {
    /// 前台应用名称，如 "Feishu"
    pub app_name: Option<String>,
    /// macOS Bundle ID，如 "com.feishu.feishu"
    pub app_bundle_id: Option<String>,
    /// 窗口标题
    pub win_title: Option<String>,
    /// 如果前台是浏览器，当前页面 URL。
    pub url: Option<String>,
    /// 如果前台是浏览器，当前页面标题。
    pub webpage_title: Option<String>,
    /// 当前焦点元素的 AX Role，如 "AXTextField"（用于密码框检测）
    pub focused_role: Option<String>,
    /// 当前焦点元素的标识符（用于执行器精确定位）
    pub focused_id: Option<String>,
    /// OCR 之前通过程序化通道提取的文本。
    ///
    /// 历史上命名为 AX 文本，但实际可能来自 macOS Accessibility Tree、
    /// 浏览器 AppleScript fallback 执行的 DOM `innerText`，或应用专用提取器。
    /// 如果为空，调用方会降级到 OCR。
    pub extracted_text: Option<String>,
}

fn parse_frontmost_basic_fields(raw: &str) -> (Option<String>, Option<String>) {
    let mut parts = raw.splitn(2, "__MB_AX_FIELD__");
    let normalized = |value: Option<&str>| {
        value
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToString::to_string)
    };
    (normalized(parts.next()), normalized(parts.next()))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum TextExtractor {
    Generic,
    Chrome,
    Safari,
    WeChat,
}

impl TextExtractor {
    fn as_str(self) -> &'static str {
        match self {
            Self::Generic => "generic",
            Self::Chrome => "chrome",
            Self::Safari => "safari",
            Self::WeChat => "wechat",
        }
    }
}

#[derive(Debug, Clone)]
struct ExtractedText {
    source: TextExtractor,
    text: String,
}

fn fallback_extractor_for_context(
    bundle_id: Option<&str>,
    app_name: Option<&str>,
) -> Option<TextExtractor> {
    match bundle_id {
        Some("com.google.Chrome")
        | Some("com.google.Chrome.canary")
        | Some("com.microsoft.edgemac")
        | Some("com.brave.Browser")
        | Some("org.chromium.Chromium")
        | Some("com.vivaldi.Vivaldi") => return Some(TextExtractor::Chrome),
        Some("com.apple.Safari") => return Some(TextExtractor::Safari),
        Some("com.tencent.xinWeChat") => return Some(TextExtractor::WeChat),
        _ => {}
    }

    match app_name {
        Some("Google Chrome")
        | Some("Google Chrome Canary")
        | Some("Microsoft Edge")
        | Some("Brave Browser")
        | Some("Chromium")
        | Some("Vivaldi") => Some(TextExtractor::Chrome),
        Some("Safari") => Some(TextExtractor::Safari),
        Some("WeChat") | Some("微信") => Some(TextExtractor::WeChat),
        _ => None,
    }
}

fn browser_identity_is_consistent(bundle_id: Option<&str>, app_name: Option<&str>) -> bool {
    let normalized_name = app_name.unwrap_or_default().trim().to_ascii_lowercase();
    match bundle_id {
        Some("com.google.Chrome") | Some("com.google.Chrome.canary") => {
            normalized_name.contains("chrome")
        }
        Some("com.microsoft.edgemac") => normalized_name.contains("edge"),
        Some("com.brave.Browser") => normalized_name.contains("brave"),
        Some("org.chromium.Chromium") => normalized_name.contains("chromium"),
        Some("com.vivaldi.Vivaldi") => normalized_name.contains("vivaldi"),
        Some("com.apple.Safari") => normalized_name == "safari",
        _ => true,
    }
}

fn chromium_app_name(bundle_id: Option<&str>, app_name: Option<&str>) -> Option<&'static str> {
    match bundle_id {
        Some("com.google.Chrome") => return Some("Google Chrome"),
        Some("com.google.Chrome.canary") => return Some("Google Chrome Canary"),
        Some("com.microsoft.edgemac") => return Some("Microsoft Edge"),
        Some("com.brave.Browser") => return Some("Brave Browser"),
        Some("org.chromium.Chromium") => return Some("Chromium"),
        Some("com.vivaldi.Vivaldi") => return Some("Vivaldi"),
        _ => {}
    }
    match app_name {
        Some("Google Chrome") => Some("Google Chrome"),
        Some("Google Chrome Canary") => Some("Google Chrome Canary"),
        Some("Microsoft Edge") => Some("Microsoft Edge"),
        Some("Brave Browser") => Some("Brave Browser"),
        Some("Chromium") => Some("Chromium"),
        Some("Vivaldi") => Some("Vivaldi"),
        _ => None,
    }
}

fn parse_keyed_quoted_value(raw: &str, key: &str) -> Option<String> {
    raw.lines().find_map(|line| {
        let line = line.trim();
        let prefix = format!("\"{key}\"=\"");
        line.strip_prefix(&prefix)
            .and_then(|value| value.strip_suffix('"'))
            .map(ToString::to_string)
            .filter(|value| !value.is_empty())
    })
}

fn normalize_whitespace(raw: &str) -> String {
    raw.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn normalize_for_comparison(raw: &str) -> String {
    normalize_whitespace(raw).to_lowercase()
}

fn is_code_symbol(ch: char) -> bool {
    matches!(
        ch,
        '{' | '}'
            | '('
            | ')'
            | '['
            | ']'
            | '<'
            | '>'
            | '.'
            | ','
            | ';'
            | ':'
            | '_'
            | '-'
            | '='
            | '+'
            | '*'
            | '/'
            | '\\'
            | '|'
            | '&'
            | '!'
            | '?'
            | '#'
            | '@'
            | '$'
            | '%'
            | '^'
            | '~'
            | '`'
            | '"'
            | '\''
    )
}

fn sanitize_extracted_text(raw: &str, win_title: Option<&str>) -> Option<String> {
    sanitize_extracted_text_with_reason(raw, win_title).ok()
}

fn sanitize_extracted_text_with_reason(
    raw: &str,
    win_title: Option<&str>,
) -> Result<String, &'static str> {
    let normalized = normalize_whitespace(raw);
    if normalized.is_empty() {
        return Err("empty_after_normalize");
    }

    let char_count = normalized.chars().count();
    if char_count < 12 {
        return Err("too_short");
    }

    let non_whitespace_count = normalized.chars().filter(|ch| !ch.is_whitespace()).count();
    if non_whitespace_count == 0 {
        return Err("no_non_whitespace");
    }

    let meaningful_count = normalized
        .chars()
        .filter(|ch| !ch.is_whitespace() && (ch.is_alphanumeric() || is_code_symbol(*ch)))
        .count();
    if (meaningful_count as f32 / non_whitespace_count as f32) < 0.55 {
        return Err("low_meaningful_char_ratio");
    }

    let normalized_cmp = normalize_for_comparison(&normalized);
    if let Some(title) = win_title {
        let title_cmp = normalize_for_comparison(title);
        if !title_cmp.is_empty() {
            if normalized_cmp == title_cmp {
                return Err("same_as_window_title");
            }

            if let Some(remaining) = normalized_cmp.strip_prefix(&title_cmp) {
                if remaining.trim().chars().count() < 8 {
                    return Err("only_window_title_plus_tiny_tail");
                }
            }
        }
    }

    let tokens: Vec<&str> = normalized.split_whitespace().collect();
    if !tokens.is_empty() {
        let short_token_count = tokens
            .iter()
            .filter(|token| token.chars().count() <= 4)
            .count();
        if tokens.len() <= 6 && short_token_count == tokens.len() && char_count < 24 {
            return Err("short_fragment_only");
        }
    }

    Ok(normalized)
}

/// 获取当前前台应用的 AX 信息（同步版本，已废弃）。
///
/// 失败（无权限 / AX 不支持 / 超时）时返回 None，由调用方降级到 OCR。
#[deprecated(note = "使用 get_frontmost_info_async 替代")]
pub fn get_frontmost_info() -> Option<AXInfo> {
    #[cfg(all(target_os = "macos", not(test)))]
    {
        macos_impl::get_frontmost_info_macos()
    }
    #[cfg(any(not(target_os = "macos"), test))]
    {
        None
    }
}

/// 检查 AX 调用是否处于熔断冷却期。
///
/// `true` 表示当前已连续超时 ≥ `MAX_CONSECUTIVE_TIMEOUTS` 次且仍在冷却窗口内。
/// 该函数仅做只读检查，不修改任何状态，供外部门禁联动使用。
pub fn is_circuit_breaker_tripped() -> bool {
    if TIMEOUT_COUNTER.load(Ordering::Relaxed) < MAX_CONSECUTIVE_TIMEOUTS {
        return false;
    }
    let last_reset =
        LAST_RESET_TIME.get_or_init(|| std::sync::Mutex::new(std::time::Instant::now()));
    match last_reset.lock() {
        Ok(guard) => guard.elapsed().as_secs() < CIRCUIT_BREAKER_COOLDOWN_SECS,
        Err(_) => false,
    }
}

/// 读取轻量前台上下文：应用身份，以及浏览器处于前台时的 URL/标题。
///
/// 不遍历 AX 树、不读取页面正文，供高频变化监听使用。
pub async fn get_frontmost_context_snapshot_async() -> Option<AXInfo> {
    #[cfg(all(target_os = "macos", not(test)))]
    {
        use std::time::Duration;

        let task = tokio::task::spawn_blocking(macos_impl::get_frontmost_context_snapshot_macos);
        match tokio::time::timeout(Duration::from_millis(1500), task).await {
            Ok(Ok(info)) => info,
            _ => None,
        }
    }
    #[cfg(any(not(target_os = "macos"), test))]
    {
        None
    }
}

/// 异步获取当前前台应用的 AX 信息（带超时保护）。
///
/// 使用 spawn_blocking 避免阻塞 tokio 运行时，基础上下文与文本提取分阶段超时。
pub async fn get_frontmost_info_async() -> Option<AXInfo> {
    #[cfg(all(target_os = "macos", not(test)))]
    {
        use std::time::{Duration, Instant};
        use tracing::{debug, warn};

        // 熔断检查：如果连续超时次数过多，进入冷却期
        let timeout_count = TIMEOUT_COUNTER.load(Ordering::Relaxed);
        if timeout_count >= MAX_CONSECUTIVE_TIMEOUTS {
            let last_reset =
                LAST_RESET_TIME.get_or_init(|| std::sync::Mutex::new(std::time::Instant::now()));
            let mut guard = last_reset.lock().unwrap();

            if guard.elapsed().as_secs() < CIRCUIT_BREAKER_COOLDOWN_SECS {
                warn!(
                    timeout_count,
                    cooldown_remaining = CIRCUIT_BREAKER_COOLDOWN_SECS - guard.elapsed().as_secs(),
                    "AX 调用熔断中，跳过本次采集"
                );
                return None;
            } else {
                // 冷却期结束，重置计数器
                TIMEOUT_COUNTER.store(0, Ordering::Relaxed);
                *guard = std::time::Instant::now();
                debug!("AX 熔断器已重置");
            }
        }

        let basic_task = tokio::task::spawn_blocking(macos_impl::get_frontmost_basic_info_macos);
        let mut info = match tokio::time::timeout(Duration::from_millis(4000), basic_task).await {
            Ok(Ok(Some(info))) => {
                debug!(
                    app = ?info.app_name,
                    bundle_id = ?info.app_bundle_id,
                    win_title = ?info.win_title,
                    "AX 基础上下文获取成功"
                );
                // 成功则重置超时计数器
                TIMEOUT_COUNTER.store(0, Ordering::Relaxed);
                info
            }
            Ok(Ok(None)) => {
                warn!("AX 基础上下文获取失败");
                TIMEOUT_COUNTER.fetch_add(1, Ordering::Relaxed);
                return None;
            }
            Ok(Err(e)) => {
                warn!("AX 基础上下文任务失败: {}", e);
                TIMEOUT_COUNTER.fetch_add(1, Ordering::Relaxed);
                return None;
            }
            Err(_) => {
                warn!("AX 基础上下文获取超时（4000ms）");
                TIMEOUT_COUNTER.fetch_add(1, Ordering::Relaxed);
                return None;
            }
        };

        let app_name = info.app_name.clone();
        let bundle_id = info.app_bundle_id.clone();
        let win_title = info.win_title.clone();
        let fallback = fallback_extractor_for_context(bundle_id.as_deref(), app_name.as_deref());
        debug!(
            app = ?app_name,
            bundle_id = ?bundle_id,
            fallback = fallback.map(|extractor| extractor.as_str()),
            "AX 文本提取策略已确定：generic-first"
        );

        let text_task = tokio::task::spawn_blocking(move || {
            macos_impl::extract_ax_text_for_context(
                app_name.as_deref(),
                bundle_id.as_deref(),
                win_title.as_deref(),
            )
        });

        match tokio::time::timeout(Duration::from_millis(1200), text_task).await {
            Ok(Ok(Some(result))) => {
                debug!(
                    source = result.source.as_str(),
                    text_len = result.text.len(),
                    "AX 文本提取成功"
                );
                info.extracted_text = Some(result.text);
            }
            Ok(Ok(None)) => {
                debug!("AX 文本提取为空或未通过质量门槛，等待 OCR 兜底");
            }
            Ok(Err(e)) => {
                warn!("AX 文本提取任务失败: {}", e);
                TIMEOUT_COUNTER.fetch_add(1, Ordering::Relaxed);
            }
            Err(_) => {
                warn!("AX 文本提取超时（1200ms）");
            }
        }

        Some(info)
    }
    #[cfg(any(not(target_os = "macos"), test))]
    {
        None
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// macOS 实现
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(all(target_os = "macos", not(test)))]
mod macos_impl {
    use super::{
        browser_identity_is_consistent, chromium_app_name, fallback_extractor_for_context,
        parse_keyed_quoted_value, sanitize_extracted_text_with_reason, AXInfo, ExtractedText,
        TextExtractor, AX_CACHE_TTL_SECS, AX_SUPPORT_CACHE, EXTRACTED_TEXT_MAX_CHARS,
        GENERIC_ALL_UI_ITEM_LIMIT, GENERIC_FOCUS_MIN_CHARS, GENERIC_STATIC_ITEM_LIMIT,
        GENERIC_WINDOW_MIN_CHARS,
    };
    use std::{
        collections::HashMap,
        process::{Command, Stdio},
        sync::{Mutex, OnceLock},
        thread,
        time::{Duration, Instant},
    };
    use tracing::{debug, warn};

    fn run_osascript(script: &str, stage: &str) -> Result<String, String> {
        let output = Command::new("osascript")
            .arg("-e")
            .arg(script)
            .output()
            .map_err(|e| format!("启动 osascript 失败: {e}"))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            let status = output
                .status
                .code()
                .map_or_else(|| "signal".to_string(), |c| c.to_string());
            return Err(format!("stage={stage} exit={status} stderr={stderr}"));
        }

        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    fn run_osascript_with_timeout(
        script: &str,
        stage: &str,
        timeout: Duration,
    ) -> Result<String, String> {
        let mut child = Command::new("osascript")
            .arg("-e")
            .arg(script)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("启动 osascript 失败: {e}"))?;

        let start = Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(_)) => {
                    let output = child
                        .wait_with_output()
                        .map_err(|e| format!("等待 osascript 输出失败: {e}"))?;
                    if !output.status.success() {
                        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                        let status = output
                            .status
                            .code()
                            .map_or_else(|| "signal".to_string(), |c| c.to_string());
                        return Err(format!("stage={stage} exit={status} stderr={stderr}"));
                    }
                    return Ok(String::from_utf8_lossy(&output.stdout).trim().to_string());
                }
                Ok(None) => {
                    if start.elapsed() >= timeout {
                        let _ = child.kill();
                        let _ = child.wait();
                        return Err(format!("stage={stage} timeout={}ms", timeout.as_millis()));
                    }
                    thread::sleep(Duration::from_millis(10));
                }
                Err(e) => return Err(format!("stage={stage} try_wait_failed: {e}")),
            }
        }
    }

    fn run_lsappinfo(args: &[&str], stage: &str) -> Result<String, String> {
        let output = Command::new("lsappinfo")
            .args(args)
            .output()
            .map_err(|e| format!("启动 lsappinfo 失败: {e}"))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
            let status = output
                .status
                .code()
                .map_or_else(|| "signal".to_string(), |c| c.to_string());
            return Err(format!("stage={stage} exit={status} stderr={stderr}"));
        }

        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    }

    fn read_frontmost_app_identity() -> Option<(String, String, Option<String>)> {
        let front = match run_lsappinfo(&["front"], "front_context") {
            Ok(raw) => raw,
            Err(err) => {
                warn!("AX front app 查询失败: {}", err);
                return None;
            }
        };

        let asn = front.trim().to_string();
        if asn.is_empty() {
            warn!("AX front app 查询未返回 ASN");
            return None;
        }

        let info_raw = match run_lsappinfo(
            &["info", "-only", "bundleID", "-only", "name", &asn],
            "front_context_info",
        ) {
            Ok(raw) => raw,
            Err(err) => {
                warn!(asn = %asn, "AX front app 信息查询失败: {}", err);
                return None;
            }
        };

        let app_name = parse_keyed_quoted_value(&info_raw, "LSDisplayName")?;
        let app_bundle_id = parse_keyed_quoted_value(&info_raw, "CFBundleIdentifier");
        if !browser_identity_is_consistent(app_bundle_id.as_deref(), Some(&app_name)) {
            warn!(
                app = %app_name,
                bundle_id = ?app_bundle_id,
                "前台浏览器身份不一致，丢弃本轮上下文快照"
            );
            return None;
        }

        Some((asn, app_name, app_bundle_id))
    }

    fn frontmost_context_is_stable(expected_asn: &str) -> bool {
        match run_lsappinfo(&["front"], "front_context_verify") {
            Ok(current) => current.trim() == expected_asn,
            Err(_) => false,
        }
    }

    pub fn get_frontmost_context_snapshot_macos() -> Option<AXInfo> {
        let (asn, app_name, app_bundle_id) = read_frontmost_app_identity()?;
        let is_browser = matches!(
            fallback_extractor_for_context(app_bundle_id.as_deref(), Some(&app_name)),
            Some(TextExtractor::Chrome | TextExtractor::Safari)
        );
        let (url, webpage_title) =
            get_browser_page_metadata(app_bundle_id.as_deref(), Some(&app_name))
                .map(|(url, title)| (Some(url), Some(title)))
                .unwrap_or((None, None));

        if is_browser && !frontmost_context_is_stable(&asn) {
            debug!("轻量上下文读取期间前台应用发生变化，丢弃本轮快照");
            return None;
        }

        Some(AXInfo {
            app_name: Some(app_name),
            app_bundle_id,
            win_title: webpage_title.clone(),
            url,
            webpage_title,
            ..Default::default()
        })
    }

    pub fn get_frontmost_info_macos() -> Option<AXInfo> {
        let mut info = get_frontmost_basic_info_macos()?;
        let app_name = info.app_name.clone();
        let app_bundle_id = info.app_bundle_id.clone();
        let win_title = info.win_title.clone();
        if let Some((url, title)) =
            get_browser_page_metadata(app_bundle_id.as_deref(), app_name.as_deref())
        {
            info.url = Some(url);
            info.webpage_title = Some(title);
        }
        info.extracted_text = extract_ax_text_for_context(
            app_name.as_deref(),
            app_bundle_id.as_deref(),
            win_title.as_deref(),
        )
        .map(|result| result.text);
        Some(info)
    }

    /// 快速检测应用是否支持 AX 文本提取（< 50ms）
    ///
    /// 通过尝试获取窗口标题来判断，比 count UI elements 更快
    /// 快速检测应用是否支持 AX 文本提取（< 50ms）
    ///
    /// 使用缓存避免重复检测同一应用
    fn check_ax_support(app_name: Option<&str>) -> bool {
        let name = match app_name {
            Some(n) => n,
            None => return false,
        };

        // 检查缓存
        let cache = AX_SUPPORT_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
        if let Ok(mut cache_guard) = cache.lock() {
            if let Some((supported, timestamp)) = cache_guard.get(name) {
                // 缓存未过期
                if timestamp.elapsed().as_secs() < AX_CACHE_TTL_SECS {
                    debug!(app = name, cached_result = supported, "使用 AX 支持缓存");
                    return *supported;
                }
            }
        }

        // 执行检测
        let check_script = format!(
            r#"tell application "System Events"
    tell process "{}"
        try
            return name of front window
        on error
            return ""
        end try
    end tell
end tell"#,
            name.replace('"', "\\\"")
        );

        let supported = match run_osascript_with_timeout(
            &check_script,
            "ax_support_check",
            Duration::from_millis(50),
        ) {
            Ok(result) => !result.trim().is_empty(),
            Err(_) => false,
        };

        // 更新缓存
        if let Ok(mut cache_guard) = cache.lock() {
            cache_guard.insert(name.to_string(), (supported, Instant::now()));
            debug!(app = name, supported, "AX 支持检测完成并缓存");
        }

        supported
    }

    pub fn get_frontmost_basic_info_macos() -> Option<AXInfo> {
        let (asn, app_name, app_bundle_id) = read_frontmost_app_identity()?;

        let basic_script = r#"
            tell application "System Events"
                set front_process to first application process whose frontmost is true
                set win_title to ""
                set focused_role to ""
                try
                    set win_title to name of front window of front_process
                end try
                try
                    set focused_element to value of attribute "AXFocusedUIElement" of front_process
                    set focused_role to role of focused_element
                end try
                return win_title & "__MB_AX_FIELD__" & focused_role
            end tell
        "#;

        let (win_title, focused_role) = match run_osascript_with_timeout(
            basic_script,
            "front_window_title",
            Duration::from_millis(1200),
        ) {
            Ok(raw) => super::parse_frontmost_basic_fields(&raw),
            Err(err) => {
                debug!(asn = %asn, "AX 窗口标题脚本失败，继续使用 app 基础信息: {}", err);
                (None, None)
            }
        };

        let (url, webpage_title) =
            get_browser_page_metadata(app_bundle_id.as_deref(), Some(&app_name))
                .map(|(url, title)| (Some(url), Some(title)))
                .unwrap_or((None, None));

        if !frontmost_context_is_stable(&asn) {
            debug!("完整上下文读取期间前台应用发生变化，丢弃本轮快照");
            return None;
        }

        Some(AXInfo {
            app_name: Some(app_name),
            app_bundle_id,
            win_title,
            url,
            webpage_title,
            focused_role,
            ..Default::default()
        })
    }

    fn get_browser_page_metadata(
        bundle_id: Option<&str>,
        app_name: Option<&str>,
    ) -> Option<(String, String)> {
        match fallback_extractor_for_context(bundle_id, app_name)? {
            TextExtractor::Chrome => {
                get_chromium_page_metadata(chromium_app_name(bundle_id, app_name)?)
            }
            TextExtractor::Safari => get_safari_page_metadata(),
            _ => None,
        }
    }

    fn get_chromium_page_metadata(app_name: &str) -> Option<(String, String)> {
        let script = format!(
            r#"
            tell application "{app_name}"
                if (count of windows) > 0 then
                    set front_win to front window
                    if (count of tabs of front_win) > 0 then
                        set active_tab to active tab of front_win
                        return (URL of active_tab) & linefeed & (title of active_tab)
                    end if
                end if
            end tell
            return ""
        "#,
            app_name = app_name,
        );

        run_browser_metadata_script(&script, "chromium_page_metadata")
    }

    fn get_safari_page_metadata() -> Option<(String, String)> {
        let script = r#"
            tell application "Safari"
                if (count of windows) > 0 then
                    set front_win to front window
                    if (count of tabs of front_win) > 0 then
                        set active_tab to current tab of front_win
                        return (URL of active_tab) & linefeed & (name of active_tab)
                    end if
                end if
            end tell
            return ""
        "#;

        run_browser_metadata_script(script, "safari_page_metadata")
    }

    fn run_browser_metadata_script(script: &str, stage: &str) -> Option<(String, String)> {
        let started = Instant::now();
        match run_osascript_with_timeout(script, stage, Duration::from_millis(600)) {
            Ok(raw) => {
                let elapsed_ms = started.elapsed().as_millis();
                match parse_page_metadata(&raw) {
                    Some((url, title)) => {
                        debug!(
                            stage,
                            elapsed_ms,
                            url_len = url.len(),
                            "浏览器 URL 提取成功"
                        );
                        Some((url, title))
                    }
                    None => {
                        warn!(
                            stage,
                            elapsed_ms,
                            raw_len = raw.len(),
                            raw_preview = %raw.chars().take(200).collect::<String>(),
                            "浏览器 URL 解析失败：osascript 返回内容不符合预期"
                        );
                        None
                    }
                }
            }
            Err(err) => {
                let elapsed_ms = started.elapsed().as_millis();
                warn!(stage, elapsed_ms, error = %err, "浏览器 URL 提取失败");
                None
            }
        }
    }

    fn parse_page_metadata(raw: &str) -> Option<(String, String)> {
        let mut lines = raw.lines().map(str::trim).filter(|line| !line.is_empty());
        let url = lines.next()?.to_string();
        if !url.starts_with("http://") && !url.starts_with("https://") {
            return None;
        }
        let title = lines.next().unwrap_or("").trim().to_string();
        Some((url, title))
    }

    pub fn extract_ax_text_for_context(
        app_name: Option<&str>,
        bundle_id: Option<&str>,
        win_title: Option<&str>,
    ) -> Option<ExtractedText> {
        debug!(
            app = ?app_name,
            bundle_id = ?bundle_id,
            "开始 AX generic-first 文本提取"
        );

        let fallback = fallback_extractor_for_context(bundle_id, app_name);

        // 快速检测 AX 支持（带缓存，首次 50ms，后续 <1ms）。
        // Chrome/Safari 的页面文本来自浏览器 AppleScript，不依赖 System Events 的 AX 树；
        // 因此 AX 探测失败时仍应尝试浏览器 fallback。
        if !check_ax_support(app_name) {
            if matches!(
                fallback,
                Some(TextExtractor::Chrome) | Some(TextExtractor::Safari)
            ) {
                debug!(
                    app = ?app_name,
                    bundle_id = ?bundle_id,
                    fallback = fallback.map(|extractor| extractor.as_str()),
                    "AX 快速检测失败，改走浏览器 fallback 文本提取"
                );
                return extract_fallback_text(fallback.unwrap(), app_name, bundle_id, win_title);
            }

            debug!(
                app = ?app_name,
                "AX 快速检测失败，应用不支持或无响应，直接降级 OCR"
            );
            return None;
        }

        // 尝试 generic 提取
        match extract_generic_text() {
            Some(raw) => match sanitize_extracted_text_with_reason(&raw, win_title) {
                Ok(text) => {
                    return Some(ExtractedText {
                        source: TextExtractor::Generic,
                        text,
                    });
                }
                Err(reason) => {
                    debug!(
                        app = ?app_name,
                        bundle_id = ?bundle_id,
                        raw_len = raw.chars().count(),
                        reason,
                        "generic AX 文本未通过质量门槛，继续尝试 fallback"
                    );
                }
            },
            None => {
                debug!(
                    app = ?app_name,
                    bundle_id = ?bundle_id,
                    reason = "generic_empty_or_failed",
                    "generic AX 文本提取为空，继续尝试 fallback"
                );
            }
        }

        // generic 失败，尝试专用提取器
        let Some(fallback) = fallback else {
            debug!(
                app = ?app_name,
                bundle_id = ?bundle_id,
                reason = "no_fallback_extractor",
                "AX 文本提取为空，将降级 OCR"
            );
            return None;
        };
        debug!(
            app = ?app_name,
            bundle_id = ?bundle_id,
            fallback = fallback.as_str(),
            "generic 未命中质量门槛，尝试 fallback"
        );

        extract_fallback_text(fallback, app_name, bundle_id, win_title)
    }

    fn extract_fallback_text(
        fallback: TextExtractor,
        app_name: Option<&str>,
        bundle_id: Option<&str>,
        win_title: Option<&str>,
    ) -> Option<ExtractedText> {
        let fallback_text = match fallback {
            TextExtractor::Generic => None,
            TextExtractor::Chrome => extract_chromium_text(chromium_app_name(bundle_id, app_name)?),
            TextExtractor::Safari => extract_safari_text(),
            TextExtractor::WeChat => extract_wechat_text(),
        };
        let Some(fallback_text) = fallback_text else {
            debug!(
                app = ?app_name,
                bundle_id = ?bundle_id,
                fallback = fallback.as_str(),
                reason = "fallback_empty_or_failed",
                "fallback 文本提取为空，将降级 OCR"
            );
            return None;
        };

        let text = match sanitize_extracted_text_with_reason(&fallback_text, win_title) {
            Ok(text) => text,
            Err(reason) => {
                debug!(
                    app = ?app_name,
                    bundle_id = ?bundle_id,
                    fallback = fallback.as_str(),
                    raw_len = fallback_text.chars().count(),
                    reason,
                    "fallback 文本未通过质量门槛，将降级 OCR"
                );
                return None;
            }
        };
        Some(ExtractedText {
            source: fallback,
            text,
        })
    }

    /// 提取 Chromium 系浏览器的页面文本
    fn extract_chromium_text(app_name: &str) -> Option<String> {
        let script = format!(
            r#"
            tell application "{app_name}"
                if (count of windows) > 0 then
                    set front_win to front window
                    if (count of tabs of front_win) > 0 then
                        set active_tab to active tab of front_win
                        try
                            set page_text to execute active_tab javascript "
                                (function() {{
                                    var title = document.title;
                                    var body = document.body;
                                    if (!body) return title;

                                    var clone = body.cloneNode(true);
                                    var scripts = clone.getElementsByTagName('script');
                                    var styles = clone.getElementsByTagName('style');
                                    for (var i = scripts.length - 1; i >= 0; i--) {{
                                        scripts[i].remove();
                                    }}
                                    for (var i = styles.length - 1; i >= 0; i--) {{
                                        styles[i].remove();
                                    }}

                                    var text = clone.innerText || clone.textContent || '';
                                    text = text.replace(/\\s+/g, ' ').trim();
                                    if (text.length > {max_chars}) {{
                                        text = text.substring(0, {max_chars}) + '...';
                                    }}
                                    return title + '\\n\\n' + text;
                                }})()
                            "
                            return page_text
                        end try
                    end if
                end if
            end tell
            return ""
        "#,
            app_name = app_name,
            max_chars = EXTRACTED_TEXT_MAX_CHARS,
        );

        match run_osascript(&script, "chromium_text") {
            Ok(text) if !text.is_empty() => Some(text),
            Ok(_) => None,
            Err(err) => {
                debug!("Chromium 浏览器 AX 文本提取失败: {}", err);
                None
            }
        }
    }

    /// 提取 Safari 浏览器的页面文本
    fn extract_safari_text() -> Option<String> {
        let script = format!(
            r#"
            tell application "Safari"
                if (count of windows) > 0 then
                    set front_win to front window
                    if (count of tabs of front_win) > 0 then
                        set active_tab to current tab of front_win
                        try
                            set page_text to do JavaScript "
                                (function() {{
                                    var title = document.title;
                                    var body = document.body;
                                    if (!body) return title;
                                    var text = body.innerText || body.textContent || '';
                                    text = text.replace(/\\s+/g, ' ').trim();
                                    if (text.length > {max_chars}) {{
                                        text = text.substring(0, {max_chars}) + '...';
                                    }}
                                    return title + '\\n\\n' + text;
                                }})()
                            " in active_tab
                            return page_text
                        end try
                    end if
                end if
            end tell
            return ""
        "#,
            max_chars = EXTRACTED_TEXT_MAX_CHARS,
        );

        match run_osascript(&script, "safari_text") {
            Ok(text) if !text.is_empty() => Some(text),
            Ok(_) => None,
            Err(err) => {
                debug!("Safari AX 文本提取失败: {}", err);
                None
            }
        }
    }

    /// 提取 WeChat 的聊天文本（作为 fallback，仅做聊天正文聚合）
    fn extract_wechat_text() -> Option<String> {
        let script = format!(
            r#"
            tell application "System Events"
                set front_process to first application process whose frontmost is true
                if name of front_process is not "WeChat" and name of front_process is not "微信" then
                    return ""
                end if

                set text_content to ""
                try
                    set front_win to front window of front_process
                    set static_items to entire contents of front_win whose role is in {{"AXStaticText", "AXTextArea", "AXTextField"}}
                    set item_count to count of static_items
                    if item_count > {limit} then set item_count to {limit}

                    repeat with idx from 1 to item_count
                        try
                            set ui_elem to item idx of static_items
                            if value of ui_elem is not missing value then
                                set val to value of ui_elem as string
                                if val is not "" then
                                    set text_content to text_content & val & linefeed
                                end if
                            end if
                        end try
                        if (length of text_content) > {max_chars} then exit repeat
                    end repeat
                end try

                if (length of text_content) > {max_chars} then
                    return text 1 thru {max_chars} of text_content
                end if
                return text_content
            end tell
        "#,
            limit = GENERIC_ALL_UI_ITEM_LIMIT,
            max_chars = EXTRACTED_TEXT_MAX_CHARS,
        );

        match run_osascript(&script, "wechat_text") {
            Ok(text) if !text.trim().is_empty() => Some(text.trim().to_string()),
            Ok(_) => None,
            Err(err) => {
                debug!("WeChat AX 文本提取失败: {}", err);
                None
            }
        }
    }

    fn extract_generic_text() -> Option<String> {
        let script = format!(
            r#"
            tell application "System Events"
                set front_process to first application process whose frontmost is true
                set text_content to ""
                set focus_text to ""
                try
                    -- 优先选择标准窗口（VSCode 等应用有多个窗口）
                    set front_win to missing value
                    try
                        set front_win to first window of front_process whose subrole is "AXStandardWindow"
                    on error
                        set front_win to front window of front_process
                    end try

                    try
                        set focused_elem to focused UI element of front_process
                        try
                            if value of focused_elem is not missing value then
                                set focus_text to value of focused_elem as string
                            end if
                        end try
                        if focus_text is "" then
                            try
                                set focus_text to description of focused_elem as string
                            end try
                        end if
                    end try

                    if (length of focus_text) ≥ {focus_min_chars} then
                        set text_content to focus_text
                    end if

                    if (length of text_content) < {window_min_chars} then
                        try
                            set static_items to entire contents of front_win whose role is in {{"AXStaticText", "AXTextArea", "AXTextField"}}
                            set item_count to count of static_items
                            if item_count > {static_limit} then set item_count to {static_limit}

                            repeat with idx from 1 to item_count
                                try
                                    set ui_elem to item idx of static_items
                                    if value of ui_elem is not missing value then
                                        set val to value of ui_elem as string
                                        if val is not "" then
                                            set text_content to text_content & linefeed & val
                                        end if
                                    end if
                                end try
                                if (length of text_content) > {max_chars} then exit repeat
                            end repeat
                        end try
                    end if

                    -- 第二层：仅在内容仍不足时才遍历全部 UI
                    if (length of text_content) < {window_min_chars} then
                        try
                            set all_ui to entire contents of front_win
                            set total_count to count of all_ui

                            -- AX 性能测试：6000+ UI 元素仅需 0.15 秒，远快于 OCR（1.8秒）
                            -- 限制遍历数量避免超长文本，而非性能考虑
                            if total_count > {all_limit} then set total_count to {all_limit}

                            repeat with idx from 1 to total_count
                                try
                                    set ui_elem to item idx of all_ui
                                    if value of ui_elem is not missing value then
                                        set val to value of ui_elem as string
                                        if val is not "" then
                                            set text_content to text_content & linefeed & val
                                        end if
                                    end if
                                end try
                                if (length of text_content) > {max_chars} then exit repeat
                            end repeat
                        end try
                    end if
                end try

                if (length of text_content) > {max_chars} then
                    return text 1 thru {max_chars} of text_content
                end if
                return text_content
            end tell
        "#,
            focus_min_chars = GENERIC_FOCUS_MIN_CHARS,
            window_min_chars = GENERIC_WINDOW_MIN_CHARS,
            static_limit = GENERIC_STATIC_ITEM_LIMIT,
            all_limit = GENERIC_ALL_UI_ITEM_LIMIT,
            max_chars = EXTRACTED_TEXT_MAX_CHARS,
        );

        match run_osascript(&script, "generic_text") {
            Ok(text) if !text.is_empty() => Some(text),
            Ok(_) => None,
            Err(err) => {
                debug!("generic AX 文本提取失败: {}", err);
                None
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

    #[test]
    fn test_ax_info_default() {
        let info = AXInfo::default();
        assert!(info.app_name.is_none());
        assert!(info.win_title.is_none());
        assert!(info.extracted_text.is_none());
    }

    #[test]
    fn test_ax_info_partial_construction() {
        let info = AXInfo {
            app_name: Some("Feishu".into()),
            win_title: Some("工作群".into()),
            focused_role: Some("AXTextField".into()),
            ..Default::default()
        };
        assert_eq!(info.app_name.as_deref(), Some("Feishu"));
        assert_eq!(info.win_title.as_deref(), Some("工作群"));
        assert_eq!(info.focused_role.as_deref(), Some("AXTextField"));
        assert!(info.extracted_text.is_none());
    }

    #[test]
    fn test_get_frontmost_returns_none_in_test() {
        let result = get_frontmost_info();
        assert!(result.is_none(), "测试环境应返回 None");
    }

    #[test]
    fn test_ax_info_clone() {
        let info = AXInfo {
            app_name: Some("VSCode".into()),
            ..Default::default()
        };
        let cloned = info.clone();
        assert_eq!(info.app_name, cloned.app_name);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn test_frontmost_basic_fields_preserve_role_without_input_content() {
        let (title, role) = super::parse_frontmost_basic_fields("工作台__MB_AX_FIELD__AXTextField");
        assert_eq!(title.as_deref(), Some("工作台"));
        assert_eq!(role.as_deref(), Some("AXTextField"));
    }

    #[test]
    fn test_parse_keyed_quoted_value() {
        let raw = "\"CFBundleIdentifier\"=\"com.microsoft.VSCode\"\n\"LSDisplayName\"=\"Code\"";
        assert_eq!(
            parse_keyed_quoted_value(raw, "CFBundleIdentifier").as_deref(),
            Some("com.microsoft.VSCode")
        );
        assert_eq!(
            parse_keyed_quoted_value(raw, "LSDisplayName").as_deref(),
            Some("Code")
        );
        assert!(parse_keyed_quoted_value(raw, "MissingKey").is_none());
    }

    #[test]
    fn test_fallback_extractor_prefers_bundle_id() {
        assert_eq!(
            fallback_extractor_for_context(Some("com.google.Chrome"), Some("Whatever")),
            Some(TextExtractor::Chrome)
        );
        assert_eq!(
            fallback_extractor_for_context(Some("com.apple.Safari"), Some("Whatever")),
            Some(TextExtractor::Safari)
        );
        assert_eq!(
            fallback_extractor_for_context(Some("com.microsoft.edgemac"), Some("Whatever")),
            Some(TextExtractor::Chrome)
        );
        assert_eq!(
            chromium_app_name(Some("com.brave.Browser"), Some("Whatever")),
            Some("Brave Browser")
        );
        assert_eq!(
            fallback_extractor_for_context(Some("com.tencent.xinWeChat"), Some("Whatever")),
            Some(TextExtractor::WeChat)
        );
    }

    #[test]
    fn test_browser_identity_consistency_rejects_cross_app_context() {
        assert!(browser_identity_is_consistent(
            Some("com.google.Chrome"),
            Some("Google Chrome")
        ));
        assert!(!browser_identity_is_consistent(
            Some("com.google.Chrome"),
            Some("memory-bread-desktop")
        ));
        assert!(browser_identity_is_consistent(
            Some("com.apple.Safari"),
            Some("Safari")
        ));
        assert!(browser_identity_is_consistent(
            Some("com.microsoft.edgemac"),
            Some("Microsoft Edge")
        ));
        assert!(!browser_identity_is_consistent(
            Some("com.brave.Browser"),
            Some("Safari")
        ));
        assert!(browser_identity_is_consistent(
            Some("com.apple.finder"),
            Some("访达")
        ));
    }

    #[test]
    fn test_vscode_no_longer_maps_to_special_extractor() {
        assert_eq!(
            fallback_extractor_for_context(Some("com.microsoft.VSCode"), Some("Code")),
            None
        );
        assert_eq!(
            fallback_extractor_for_context(None, Some("Visual Studio Code")),
            None
        );
    }

    #[test]
    fn test_sanitize_extracted_text_rejects_short_or_title_only_text() {
        assert_eq!(sanitize_extracted_text("保存 取消", Some("设置")), None);
        assert_eq!(
            sanitize_extracted_text(
                "Investigate OCR memory f — gzdz",
                Some("Investigate OCR memory f — gzdz")
            ),
            None
        );
    }

    #[test]
    fn test_sanitize_extracted_text_accepts_meaningful_content() {
        let text = "fn process_event(event: CaptureEvent) -> Result<Option<i64>, CaptureError> { let has_ax_text = true; }";
        assert_eq!(
            sanitize_extracted_text(text, Some("engine.rs")),
            Some(normalize_whitespace(text))
        );
    }

    #[test]
    fn test_sanitize_extracted_text_rejects_low_information_token_list() {
        assert_eq!(
            sanitize_extracted_text("文件 编辑 选择 视图 运行", Some("Code")),
            None
        );
    }
}
