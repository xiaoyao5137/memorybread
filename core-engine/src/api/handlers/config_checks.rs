//! 运行环境配置检测接口

use std::{
    io::{Read, Write},
    net::{TcpStream, ToSocketAddrs},
    process::Command,
    sync::Arc,
    time::Duration,
};

use axum::{
    extract::{Path, State},
    Json,
};
use serde::Serialize;

use crate::api::{error::ApiError, state::AppState};

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConfigCheckStatus {
    Ok,
    Warning,
    Failed,
    Unsupported,
}

#[derive(Debug, Clone, Serialize)]
pub struct ConfigCheckItem {
    pub id: &'static str,
    pub name: &'static str,
    pub description: &'static str,
    pub status: ConfigCheckStatus,
    pub message: String,
    pub details: Vec<String>,
    pub can_install: bool,
    pub can_delete: bool,
}

#[derive(Debug, Serialize)]
pub struct ConfigChecksResponse {
    pub items: Vec<ConfigCheckItem>,
}

#[derive(Debug, Serialize)]
pub struct ConfigCheckActionResponse {
    pub id: String,
    pub action: String,
    pub status: ConfigCheckStatus,
    pub message: String,
    pub details: Vec<String>,
}

pub async fn list_config_checks(
    State(state): State<Arc<AppState>>,
) -> Result<Json<ConfigChecksResponse>, ApiError> {
    let sidecar_url = state.sidecar_url.clone();
    let items = tokio::task::spawn_blocking(move || run_all_checks(&sidecar_url))
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))?;

    Ok(Json(ConfigChecksResponse { items }))
}

pub async fn run_config_check(
    State(state): State<Arc<AppState>>,
    Path(id): Path<String>,
) -> Result<Json<ConfigCheckActionResponse>, ApiError> {
    let sidecar_url = state.sidecar_url.clone();
    let item = tokio::task::spawn_blocking(move || run_single_check(&id, &sidecar_url))
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(ConfigCheckActionResponse {
        id: item.id.to_string(),
        action: "verify".to_string(),
        status: item.status,
        message: item.message,
        details: item.details,
    }))
}

pub async fn install_config_check(
    Path(id): Path<String>,
) -> Result<Json<ConfigCheckActionResponse>, ApiError> {
    let action = tokio::task::spawn_blocking(move || run_config_action(&id, "install"))
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(action))
}

pub async fn delete_config_check(
    Path(id): Path<String>,
) -> Result<Json<ConfigCheckActionResponse>, ApiError> {
    let action = tokio::task::spawn_blocking(move || run_config_action(&id, "delete"))
        .await
        .map_err(|e| ApiError::Internal(e.to_string()))??;

    Ok(Json(action))
}

fn run_all_checks(sidecar_url: &str) -> Vec<ConfigCheckItem> {
    vec![
        check_accessibility_permission(),
        check_chrome_javascript_permission(),
        check_core_api(),
        check_sidecar_ocr(sidecar_url),
    ]
}

fn run_single_check(id: &str, sidecar_url: &str) -> Result<ConfigCheckItem, ApiError> {
    match id {
        "accessibility" => Ok(check_accessibility_permission()),
        "chrome_javascript" => Ok(check_chrome_javascript_permission()),
        "core_api" => Ok(check_core_api()),
        "sidecar_ocr" => Ok(check_sidecar_ocr(sidecar_url)),
        other => Err(ApiError::NotFound(format!(
            "config check '{other}' not found"
        ))),
    }
}

fn check_accessibility_permission() -> ConfigCheckItem {
    #[cfg(target_os = "macos")]
    {
        let script = r#"
            tell application "System Events"
                set front_process to first application process whose frontmost is true
                return name of front_process
            end tell
        "#;
        match run_osascript(script, Duration::from_secs(2)) {
            Ok(output) if !output.trim().is_empty() => ConfigCheckItem {
                id: "accessibility",
                name: "辅助功能权限",
                description: "用于读取前台应用 AX 树、窗口标题和部分应用文本。",
                status: ConfigCheckStatus::Ok,
                message: format!("已可读取前台应用：{}", output.trim()),
                details: vec![],
                can_install: true,
                can_delete: true,
            },
            Ok(_) => ConfigCheckItem {
                id: "accessibility",
                name: "辅助功能权限",
                description: "用于读取前台应用 AX 树、窗口标题和部分应用文本。",
                status: ConfigCheckStatus::Failed,
                message: "System Events 返回空结果，辅助功能链路不可用".to_string(),
                details: accessibility_steps(),
                can_install: true,
                can_delete: true,
            },
            Err(err) => ConfigCheckItem {
                id: "accessibility",
                name: "辅助功能权限",
                description: "用于读取前台应用 AX 树、窗口标题和部分应用文本。",
                status: ConfigCheckStatus::Failed,
                message: "无法通过 System Events 读取前台应用".to_string(),
                details: vec![err, accessibility_steps().join("\n")],
                can_install: true,
                can_delete: true,
            },
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        ConfigCheckItem {
            id: "accessibility",
            name: "辅助功能权限",
            description: "用于读取前台应用 AX 树、窗口标题和部分应用文本。",
            status: ConfigCheckStatus::Unsupported,
            message: "当前平台暂不支持 macOS AX 检测".to_string(),
            details: vec![],
            can_install: false,
            can_delete: false,
        }
    }
}

fn check_chrome_javascript_permission() -> ConfigCheckItem {
    #[cfg(target_os = "macos")]
    {
        let script = r#"
            tell application "Google Chrome"
                if (count of windows) = 0 then return "__NO_WINDOW__"
                set page_text to execute active tab of front window javascript "document.title + '\n' + ((document.body && document.body.innerText) || '').slice(0, 120)"
                return page_text
            end tell
        "#;
        match run_osascript(script, Duration::from_secs(3)) {
            Ok(output) if output.trim() == "__NO_WINDOW__" => ConfigCheckItem {
                id: "chrome_javascript",
                name: "Chrome JavaScript 自动化",
                description: "用于在 Chrome 当前标签页执行 DOM innerText 提取。",
                status: ConfigCheckStatus::Warning,
                message: "Chrome 当前没有可检测窗口".to_string(),
                details: chrome_steps(),
                can_install: true,
                can_delete: true,
            },
            Ok(output) if output.trim().chars().count() >= 12 => ConfigCheckItem {
                id: "chrome_javascript",
                name: "Chrome JavaScript 自动化",
                description: "用于在 Chrome 当前标签页执行 DOM innerText 提取。",
                status: ConfigCheckStatus::Ok,
                message: "Chrome 可执行页面 JavaScript".to_string(),
                details: vec![format!(
                    "返回预览：{}",
                    output.trim().chars().take(80).collect::<String>()
                )],
                can_install: true,
                can_delete: true,
            },
            Ok(_) => ConfigCheckItem {
                id: "chrome_javascript",
                name: "Chrome JavaScript 自动化",
                description: "用于在 Chrome 当前标签页执行 DOM innerText 提取。",
                status: ConfigCheckStatus::Warning,
                message: "Chrome JS 可执行，但当前页面返回文本过短".to_string(),
                details: chrome_steps(),
                can_install: true,
                can_delete: true,
            },
            Err(err) => ConfigCheckItem {
                id: "chrome_javascript",
                name: "Chrome JavaScript 自动化",
                description: "用于在 Chrome 当前标签页执行 DOM innerText 提取。",
                status: ConfigCheckStatus::Failed,
                message: "Chrome 无法执行页面 JavaScript".to_string(),
                details: vec![err, chrome_steps().join("\n")],
                can_install: true,
                can_delete: true,
            },
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        ConfigCheckItem {
            id: "chrome_javascript",
            name: "Chrome JavaScript 自动化",
            description: "用于在 Chrome 当前标签页执行 DOM innerText 提取。",
            status: ConfigCheckStatus::Unsupported,
            message: "当前平台暂不支持 AppleScript 检测".to_string(),
            details: vec![],
            can_install: false,
            can_delete: false,
        }
    }
}

fn check_core_api() -> ConfigCheckItem {
    ConfigCheckItem {
        id: "core_api",
        name: "Core API",
        description: "设置页与调试面板依赖的本地 HTTP 服务。",
        status: ConfigCheckStatus::Ok,
        message: "Core API 当前可用".to_string(),
        details: vec![],
        can_install: false,
        can_delete: false,
    }
}

fn check_sidecar_ocr(sidecar_url: &str) -> ConfigCheckItem {
    match http_health_check(sidecar_url) {
        Ok(status_line) if status_line.contains(" 200 ") => ConfigCheckItem {
            id: "sidecar_ocr",
            name: "AI Sidecar / OCR",
            description: "AX 或 DOM 提取失败时的 OCR 兜底服务。",
            status: ConfigCheckStatus::Ok,
            message: "AI Sidecar 当前可用".to_string(),
            details: vec![],
            can_install: false,
            can_delete: false,
        },
        Ok(status_line) => ConfigCheckItem {
            id: "sidecar_ocr",
            name: "AI Sidecar / OCR",
            description: "AX 或 DOM 提取失败时的 OCR 兜底服务。",
            status: ConfigCheckStatus::Warning,
            message: format!("AI Sidecar 返回 {status_line}"),
            details: vec![format!("检测地址：{sidecar_url}/health")],
            can_install: false,
            can_delete: false,
        },
        Err(err) => ConfigCheckItem {
            id: "sidecar_ocr",
            name: "AI Sidecar / OCR",
            description: "AX 或 DOM 提取失败时的 OCR 兜底服务。",
            status: ConfigCheckStatus::Warning,
            message: "AI Sidecar 暂不可达".to_string(),
            details: vec![err.to_string(), format!("检测地址：{sidecar_url}/health")],
            can_install: false,
            can_delete: false,
        },
    }
}

fn http_health_check(base_url: &str) -> Result<String, String> {
    let trimmed = base_url
        .trim()
        .strip_prefix("http://")
        .unwrap_or(base_url.trim())
        .trim_end_matches('/');
    let host_port = trimmed
        .split('/')
        .next()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("无效 sidecar 地址：{base_url}"))?;
    let (host, port) = match host_port.rsplit_once(':') {
        Some((host, port)) => {
            let port = port
                .parse::<u16>()
                .map_err(|e| format!("解析端口失败: {e}"))?;
            (host, port)
        }
        None => (host_port, 80),
    };

    let addr = (host, port)
        .to_socket_addrs()
        .map_err(|e| format!("解析地址失败: {e}"))?
        .next()
        .ok_or_else(|| format!("无法解析地址：{host}:{port}"))?;
    let mut stream = TcpStream::connect_timeout(&addr, Duration::from_secs(2))
        .map_err(|e| format!("连接失败: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .map_err(|e| format!("设置读取超时失败: {e}"))?;
    stream
        .write_all(
            format!("GET /health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n").as_bytes(),
        )
        .map_err(|e| format!("发送请求失败: {e}"))?;

    let mut buf = [0_u8; 256];
    let n = stream
        .read(&mut buf)
        .map_err(|e| format!("读取响应失败: {e}"))?;
    let response = String::from_utf8_lossy(&buf[..n]);
    response
        .lines()
        .next()
        .map(|line| line.to_string())
        .ok_or_else(|| "Sidecar 响应为空".to_string())
}

fn run_config_action(id: &str, action: &str) -> Result<ConfigCheckActionResponse, ApiError> {
    match (id, action) {
        ("accessibility", "install") => {
            open_url(
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            )?;
            Ok(action_response(
                id,
                action,
                ConfigCheckStatus::Warning,
                "已打开辅助功能设置，请勾选记忆面包或当前终端/启动器。",
                accessibility_steps(),
            ))
        }
        ("accessibility", "delete") => {
            open_url(
                "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
            )?;
            Ok(action_response(
                id,
                action,
                ConfigCheckStatus::Warning,
                "已打开辅助功能设置，请手动移除或关闭记忆面包授权。",
                vec!["macOS 不允许普通应用静默移除辅助功能授权，需用户确认。".to_string()],
            ))
        }
        ("chrome_javascript", "install") => {
            open_app("Google Chrome")?;
            Ok(action_response(
                id,
                action,
                ConfigCheckStatus::Warning,
                "已打开 Chrome，请在菜单栏启用 JavaScript 自动化。",
                chrome_steps(),
            ))
        }
        ("chrome_javascript", "delete") => {
            open_url("x-apple.systempreferences:com.apple.preference.security?Privacy_Automation")?;
            Ok(action_response(
                id,
                action,
                ConfigCheckStatus::Warning,
                "已打开自动化权限设置，请手动移除 Chrome / System Events 相关授权。",
                vec![
                    "Chrome 菜单里的 Allow JavaScript from Apple Events 也可以手动关闭。"
                        .to_string(),
                    "macOS 自动化授权通常需要用户在系统设置中确认。".to_string(),
                ],
            ))
        }
        ("core_api" | "sidecar_ocr", "install" | "delete") => Ok(action_response(
            id,
            action,
            ConfigCheckStatus::Unsupported,
            "该检测项暂不支持安装或删除操作。",
            vec![],
        )),
        (other, _) => Err(ApiError::NotFound(format!(
            "config check '{other}' not found"
        ))),
    }
}

fn action_response(
    id: &str,
    action: &str,
    status: ConfigCheckStatus,
    message: &str,
    details: Vec<String>,
) -> ConfigCheckActionResponse {
    ConfigCheckActionResponse {
        id: id.to_string(),
        action: action.to_string(),
        status,
        message: message.to_string(),
        details,
    }
}

fn accessibility_steps() -> Vec<String> {
    vec![
        "系统设置 -> 隐私与安全性 -> 辅助功能。".to_string(),
        "勾选记忆面包；如果是从终端启动，也需要勾选 Terminal / iTerm / Codex。".to_string(),
        "修改后建议重启 Core Engine。".to_string(),
    ]
}

fn chrome_steps() -> Vec<String> {
    vec![
        "Chrome 菜单栏 -> View -> Developer -> Allow JavaScript from Apple Events。".to_string(),
        "如果系统弹出自动化授权，请允许当前启动器控制 Google Chrome。".to_string(),
        "企业云文档若使用跨域 iframe、canvas 或虚拟列表，仍可能需要 Chrome Extension 或 OCR 兜底。"
            .to_string(),
    ]
}

#[cfg(target_os = "macos")]
pub(super) fn run_osascript(script: &str, timeout: Duration) -> Result<String, String> {
    let mut child = Command::new("osascript")
        .arg("-e")
        .arg(script)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|e| format!("启动 osascript 失败: {e}"))?;

    let started = std::time::Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let output = child
                    .wait_with_output()
                    .map_err(|e| format!("等待 osascript 输出失败: {e}"))?;
                if !output.status.success() {
                    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
                    return Err(format!("osascript 失败: {stderr}"));
                }
                return Ok(String::from_utf8_lossy(&output.stdout).trim().to_string());
            }
            Ok(None) => {
                if started.elapsed() >= timeout {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!("osascript 超时 {}ms", timeout.as_millis()));
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(e) => return Err(format!("osascript 状态检查失败: {e}")),
        }
    }
}

pub(super) fn open_url(url: &str) -> Result<(), ApiError> {
    #[cfg(target_os = "macos")]
    {
        let status = Command::new("open")
            .arg(url)
            .status()
            .map_err(|e| ApiError::Internal(format!("打开系统设置失败: {e}")))?;
        if !status.success() { return Err(ApiError::Internal("打开系统设置失败".into())); }
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = url;
        Err(ApiError::BadRequest("当前平台不支持此操作".to_string()))
    }
}

fn open_app(app: &str) -> Result<(), ApiError> {
    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .args(["-a", app])
            .status()
            .map_err(|e| ApiError::Internal(format!("打开应用失败: {e}")))?;
        Ok(())
    }
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app;
        Err(ApiError::BadRequest("当前平台不支持此操作".to_string()))
    }
}
