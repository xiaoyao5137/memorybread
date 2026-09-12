//! Onboarding checks run in the capture process. GET never requests TCC access.
use crate::api::error::ApiError;
use axum::{extract::Path, Json};
use serde::Serialize;

#[derive(Serialize)]
pub struct PermissionItem {
    id: String,
    name: String,
    description: String,
    status: &'static str,
    message: String,
    optional: bool,
}
fn item(
    id: &str,
    name: &str,
    description: &str,
    status: &'static str,
    message: &str,
    optional: bool,
) -> PermissionItem {
    PermissionItem {
        id: id.into(),
        name: name.into(),
        description: description.into(),
        status,
        message: message.into(),
        optional,
    }
}
#[derive(Serialize)]
pub struct PermissionReport {
    items: Vec<PermissionItem>,
}

pub async fn list_permissions() -> Result<Json<PermissionReport>, ApiError> {
    tokio::task::spawn_blocking(|| PermissionReport {
        items: platform::checks(),
    })
    .await
    .map(Json)
    .map_err(|e| ApiError::Internal(e.to_string()))
}
pub async fn permission_action(
    Path((id, action)): Path<(String, String)>,
) -> Result<Json<PermissionItem>, ApiError> {
    if !matches!(action.as_str(), "request" | "verify") {
        return Err(ApiError::BadRequest("不支持的权限操作".into()));
    }
    tokio::task::spawn_blocking(move || platform::action(&id, &action))
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))?
        .map(Json)
}

#[cfg(target_os = "macos")]
mod platform {
    use super::super::config_checks::{open_url, run_osascript};
    use super::*;
    use core_foundation::{
        base::TCFType, boolean::CFBoolean, dictionary::CFDictionary, string::CFString,
    };
    use std::{path::PathBuf, time::Duration};
    #[link(name = "ApplicationServices", kind = "framework")]
    extern "C" {
        fn AXIsProcessTrusted() -> bool;
        fn AXIsProcessTrustedWithOptions(
            options: core_foundation::dictionary::CFDictionaryRef,
        ) -> bool;
        fn CGPreflightScreenCaptureAccess() -> bool;
        fn CGRequestScreenCaptureAccess() -> bool;
        fn CGPreflightListenEventAccess() -> bool;
        fn CGRequestListenEventAccess() -> bool;
    }
    // AE descriptors use the macOS SDK's two-byte packing (including on arm64).
    #[repr(C, packed(2))]
    struct AEDesc {
        descriptor_type: u32,
        data_handle: *mut std::ffi::c_void,
    }
    #[link(name = "CoreServices", kind = "framework")]
    extern "C" {
        fn AECreateDesc(
            kind: u32,
            data: *const std::ffi::c_void,
            size: std::ffi::c_long,
            result: *mut AEDesc,
        ) -> i16;
        fn AEDisposeDesc(desc: *mut AEDesc) -> i16;
        fn AEDeterminePermissionToAutomateTarget(
            target: *const AEDesc,
            event_class: u32,
            event_id: u32,
            ask: u8,
        ) -> i32;
    }
    fn automation_permission(ask: bool) -> i32 {
        automation_permission_for("com.apple.systemevents", ask)
    }
    fn automation_permission_for(bundle: &str, ask: bool) -> i32 {
        let bundle = bundle.as_bytes();
        let mut descriptor = AEDesc {
            descriptor_type: 0,
            data_handle: std::ptr::null_mut(),
        };
        unsafe {
            let result = AECreateDesc(
                u32::from_be_bytes(*b"bund"),
                bundle.as_ptr().cast(),
                bundle.len() as std::ffi::c_long,
                &mut descriptor,
            );
            if result != 0 {
                return i32::from(result);
            }
            let result = AEDeterminePermissionToAutomateTarget(
                &descriptor,
                u32::from_be_bytes(*b"core"),
                u32::from_be_bytes(*b"getd"),
                u8::from(ask),
            );
            AEDisposeDesc(&mut descriptor);
            result
        }
    }
    fn automation_status(result: i32) -> &'static str {
        match result {
            0 => "granted",
            -1743 | -1744 => "required",
            _ => "unverified",
        }
    }
    #[cfg(test)]
    mod native_tests {
        use super::*;
        #[test]
        fn browser_guidance_and_checks_are_specific_and_do_not_read_page_content() {
            assert!(browser_steps("safari").contains("Safari → 设置"));
            assert!(browser_steps("chrome").contains("View"));
            for (id, name, _) in BROWSERS {
                let script = browser_check_script(id, name);
                assert!(script.starts_with("if application"));
                assert!(script.contains("is not running"));
                assert!(!script.contains("innerText"));
                assert!(script.contains("MB_PERMISSION_READY"));
            }
            assert!(browser_check_script("safari", "Safari").contains("do JavaScript"));
            assert!(browser_check_script("chrome", "Google Chrome").contains("execute active tab"));
        }
        #[test]
        fn automation_status_never_treats_unknown_errors_as_authorized() {
            assert_eq!(automation_status(0), "granted");
            assert_eq!(automation_status(-1743), "required");
            assert_eq!(automation_status(-1744), "required");
            assert_eq!(automation_status(-600), "unverified");
            assert_eq!(std::mem::size_of::<AEDesc>(), 12);
        }
    }
    // Only browsers supported by the capture adapter are offered. Never start one on GET.
    const BROWSERS: &[(&str, &str, &str)] = &[
        ("chrome", "Google Chrome", "com.google.Chrome"),
        ("edge", "Microsoft Edge", "com.microsoft.edgemac"),
        ("brave", "Brave Browser", "com.brave.Browser"),
        (
            "chrome_canary",
            "Google Chrome Canary",
            "com.google.Chrome.canary",
        ),
        ("chromium", "Chromium", "org.chromium.Chromium"),
        ("vivaldi", "Vivaldi", "com.vivaldi.Vivaldi"),
        ("safari", "Safari", "com.apple.Safari"),
    ];
    fn installed(name: &str) -> bool {
        [
            Some(PathBuf::from("/Applications")),
            Some(PathBuf::from("/System/Applications")),
            std::env::var_os("HOME").map(|h| PathBuf::from(h).join("Applications")),
        ]
        .into_iter()
        .flatten()
        .any(|p| p.join(format!("{name}.app")).exists())
    }
    pub fn checks() -> Vec<PermissionItem> {
        let screen = unsafe { CGPreflightScreenCaptureAccess() };
        let ax = unsafe { AXIsProcessTrusted() };
        let input = unsafe { CGPreflightListenEventAccess() };
        let mut items = vec![
            item(
                "screen",
                "屏幕录制",
                "用于截图与记录屏幕内容。",
                if screen { "granted" } else { "required" },
                if screen {
                    "系统已授权。"
                } else {
                    "请在系统设置中允许记忆面包录制屏幕。"
                },
                false,
            ),
            item(
                "accessibility",
                "辅助功能",
                "用于读取应用窗口与界面文字。",
                if ax { "granted" } else { "required" },
                if ax {
                    "采集进程已获得辅助功能权限。"
                } else {
                    "请在系统设置中开启辅助功能。"
                },
                false,
            ),
            item(
                "automation",
                "应用自动化",
                "允许通过 System Events 读取当前应用文字。",
                automation_status(automation_permission(false)),
                "点击去开启将打开系统授权与自动化设置，完成后自动检查。",
                false,
            ),
            item(
                "input",
                "输入监控",
                "仅感知键鼠活动以调整采集时机，不读取或保存键值。",
                if input { "granted" } else { "required" },
                "可选；未开启时仍可使用其他采集触发方式。",
                true,
            ),
        ];
        for (id, name, bundle) in BROWSERS {
            if installed(name) {
                let status = browser_status(id, name, bundle);
                items.push(item(
                    id,
                    &format!("{name} 网页增强"),
                    "获取更完整的网页文字。",
                    status,
                    if status == "verified" {
                        "已开启网页增强。"
                    } else {
                        browser_steps(id)
                    },
                    true,
                ));
            }
        }
        items
    }
    fn browser_steps(id: &str) -> &'static str {
        if id == "safari" {
            "Safari → 设置 → 高级 → 显示网页开发者功能；再到菜单栏“开发”开启“允许来自 Apple 事件的 JavaScript”。如有系统授权提示，请允许记忆面包访问 Safari。返回后自动检查。"
        } else {
            "浏览器菜单栏“显示（View）→ 开发者（Developer）→ 允许来自 Apple 事件的 JavaScript”。如有系统授权提示，请允许记忆面包访问浏览器。返回后自动检查。"
        }
    }
    fn browser_check_script(id: &str, name: &str) -> String {
        let command = if id == "safari" {
            "do JavaScript \"'MB_PERMISSION_READY'\" in current tab of front window"
        } else {
            "execute active tab of front window javascript \"'MB_PERMISSION_READY'\""
        };
        format!(
            r#"if application "{name}" is not running then return "NOT_RUNNING"
            tell application "{name}"
                if (count of windows) = 0 then return "NO_WINDOW"
                return {command}
            end tell"#
        )
    }
    fn browser_status(id: &str, name: &str, bundle: &str) -> &'static str {
        // The native preflight never launches a browser or asks for consent. Only probe an already-authorized target.
        if automation_permission_for(bundle, false) != 0 {
            return "unverified";
        }
        match run_osascript(&browser_check_script(id, name), Duration::from_millis(700)) {
            Ok(value) if value == "MB_PERMISSION_READY" => "verified",
            _ => "unverified",
        }
    }
    pub fn action(id: &str, action: &str) -> Result<PermissionItem, ApiError> {
        let mut result = checks()
            .into_iter()
            .find(|v| v.id == id)
            .ok_or_else(|| ApiError::NotFound("未找到此权限或对应浏览器未安装".into()))?;
        if action == "request" {
            if let Some((_, name, bundle)) = BROWSERS.iter().find(|(key, _, _)| *key == id) {
                let status = std::process::Command::new("open")
                    .args(["-a", name])
                    .status()
                    .map_err(|e| ApiError::Internal(format!("打开浏览器失败: {e}")))?;
                if !status.success() {
                    return Err(ApiError::Internal(
                        "打开浏览器失败，请确认浏览器已安装".into(),
                    ));
                }
                let bundle = *bundle;
                std::thread::spawn(move || {
                    automation_permission_for(bundle, true);
                });
                result.message = format!("已打开 {name}。{}", browser_steps(id));
                return Ok(result);
            }
            let pane = match id {
                "screen" => {
                    unsafe {
                        CGRequestScreenCaptureAccess();
                    }
                    Some("Privacy_ScreenCapture")
                }
                "accessibility" => {
                    let options = CFDictionary::from_CFType_pairs(&[(
                        CFString::new("AXTrustedCheckOptionPrompt"),
                        CFBoolean::true_value(),
                    )]);
                    unsafe {
                        AXIsProcessTrustedWithOptions(options.as_concrete_TypeRef());
                    }
                    Some("Privacy_Accessibility")
                }
                "automation" => {
                    // System Events must be running for the permission API. Only launch on user action.
                    std::process::Command::new("open")
                        .args(["-g", "-b", "com.apple.systemevents"])
                        .status()
                        .map_err(|e| ApiError::Internal(format!("打开系统自动化服务失败: {e}")))?;
                    // Open settings immediately, then request registration/consent without blocking the UI response.
                    open_url("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")?;
                    std::thread::spawn(|| {
                        automation_permission(true);
                    });
                    result.message = "已打开自动化设置；如出现系统授权提示，请允许记忆面包访问 System Events。完成后将自动检查。".into();
                    return Ok(result);
                }
                "input" => {
                    unsafe {
                        CGRequestListenEventAccess();
                    }
                    Some("Privacy_ListenEvent")
                }
                _ => None,
            };
            if let Some(pane) = pane {
                open_url(&format!(
                    "x-apple.systempreferences:com.apple.preference.security?{pane}"
                ))?;
                result.message = "已打开系统设置。开启后回到记忆面包自动复查；如系统要求重启，请退出并重新打开，初始化进度会保留。".into();
                return Ok(result); // Opening settings is never proof of authorization.
            }
        }
        let verified = match id {
            "screen" if unsafe { CGPreflightScreenCaptureAccess() } => {
                xcap::Monitor::all()
                    .ok()
                    .and_then(|monitors| monitors.into_iter().next())
                    .and_then(|monitor| monitor.capture_image().ok())
                    .map(|image| image.width() > 0 && image.height() > 0)
                    .unwrap_or(false)
                // Image is dropped in memory, never persisted or returned.
            }
            "screen" => false,
            "accessibility" => unsafe { AXIsProcessTrusted() },
            "input" => unsafe { CGPreflightListenEventAccess() },
            "automation" => run_osascript(
                r#"tell application "System Events"
                tell first application process whose frontmost is true
                    if (count of windows) = 0 then return "NO_WINDOW"
                    set window_text to name of front window
                    if length of window_text = 0 then return "NO_TEXT"
                    return "VERIFIED"
                end tell
            end tell"#,
                Duration::from_secs(60),
            )
            .map(|s| s == "VERIFIED")
            .unwrap_or(false),
            _ => {
                let (_, name, bundle) = BROWSERS
                    .iter()
                    .find(|(key, _, _)| *key == id)
                    .ok_or_else(|| ApiError::NotFound("不支持的浏览器".into()))?;
                browser_status(id, name, bundle) == "verified"
            }
        };
        result.status = if verified { "verified" } else { "required" };
        result.message = if verified { "实际检测通过；未保存检测内容。" } else { "尚未验证通过。请检查系统授权并打开有文字的应用窗口或普通网页，再次验证；若已开启仍无效，请退出并重新打开记忆面包。" }.into();
        if !verified && id == "automation" {
            open_url("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")?;
        }
        Ok(result)
    }
}
#[cfg(not(target_os = "macos"))]
mod platform {
    use super::*;
    pub fn checks() -> Vec<PermissionItem> {
        vec![item(
            "platform",
            "系统权限",
            "当前平台无需使用 macOS 授权引导。",
            "unsupported",
            "如采集不可用，请检查当前系统的隐私设置。",
            true,
        )]
    }
    pub fn action(_: &str, _: &str) -> Result<PermissionItem, ApiError> {
        Err(ApiError::BadRequest("当前平台不支持此操作".into()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn rejects_unknown_action_before_platform_access() {
        assert!(
            permission_action(Path(("screen".into(), "enable_silently".into())))
                .await
                .is_err()
        );
    }
    #[tokio::test]
    async fn passive_status_uses_capture_process_without_requesting_access() {
        let report = list_permissions().await.unwrap().0;
        assert!(!report.items.is_empty());
        for entry in &report.items {
            assert!(matches!(
                entry.status,
                "granted" | "required" | "unverified" | "verified" | "unsupported"
            ));
        }
        #[cfg(target_os = "macos")]
        {
            assert!(report.items.iter().any(|entry| entry.id == "screen"));
            let automation = report
                .items
                .iter()
                .find(|entry| entry.id == "automation")
                .unwrap();
            assert!(matches!(
                automation.status,
                "granted" | "required" | "unverified"
            ));
        }
    }
    #[test]
    fn report_preserves_optional_and_unverified_states() {
        let value = serde_json::to_value(item("automation", "自动化", "", "unverified", "", false))
            .unwrap();
        assert_eq!(value["status"], "unverified");
        assert_eq!(value["optional"], false);
    }
}
