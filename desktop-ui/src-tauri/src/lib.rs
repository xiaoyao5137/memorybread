#[cfg(not(debug_assertions))]
use std::fs::OpenOptions;
#[cfg(not(feature = "app-store"))]
use std::sync::{atomic::AtomicU64, Arc};
use std::{
    fs,
    io::{Read, Write},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicI64, Ordering},
        Mutex, OnceLock,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use base64::{engine::general_purpose, Engine as _};
use image::{codecs::jpeg::JpegEncoder, imageops, DynamicImage, Rgba, RgbaImage};
use serde::{Deserialize, Serialize};
#[cfg(not(feature = "app-store"))]
use sha2::{Digest, Sha256};
use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::TrayIconBuilder,
    AppHandle, Emitter, LogicalSize, Manager, Monitor, PhysicalPosition, RunEvent, WebviewUrl,
    WebviewWindowBuilder, WindowEvent,
};
#[cfg(not(feature = "app-store"))]
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_shell::ShellExt;
#[cfg(not(feature = "app-store"))]
use tauri_plugin_updater::{Update, UpdaterExt};

#[cfg(target_os = "macos")]
use objc2::{
    define_class,
    ffi::{objc_getAssociatedObject, objc_setAssociatedObject, OBJC_ASSOCIATION_RETAIN_NONATOMIC},
    msg_send,
    rc::Retained,
    runtime::AnyObject,
    AllocAnyThread, DeclaredClass, MainThreadMarker, MainThreadOnly,
};
#[cfg(target_os = "macos")]
use objc2_app_kit::{
    NSApplication, NSEvent, NSImage, NSImageView, NSTrackingArea, NSTrackingAreaOptions, NSView,
    NSWindow, NSWindowCollectionBehavior, NSWorkspace,
};
#[cfg(target_os = "macos")]
use objc2_foundation::NSData;

static QUITTING: AtomicBool = AtomicBool::new(false);
#[cfg(debug_assertions)]
static FULL_SHUTDOWN_SCHEDULED: AtomicBool = AtomicBool::new(false);
static LAST_FLOATING_ASSIST_TEMP_CLEANUP_MS: AtomicI64 = AtomicI64::new(0);
static PACKAGED_RUNTIME_HOME: OnceLock<PathBuf> = OnceLock::new();
#[cfg(unix)]
static PACKAGED_IPC_SOCKET_PATH: OnceLock<PathBuf> = OnceLock::new();
const IPC_SOCKET_ENV: &str = "MEMORY_BREAD_IPC_SOCKET";
#[cfg(unix)]
const DEFAULT_IPC_SOCKET_PATH: &str = "/tmp/memory-bread-sidecar.sock";
const FLOATING_ASSIST_LABEL: &str = "floating-assist";
#[cfg(debug_assertions)]
const SUPERVISOR_SHUTDOWN_MARKER: &str = "supervisor-shutdown-in-progress";
const FLOATING_ASSIST_DEFAULT_MARGIN: i32 = 24;
const FLOATING_ASSIST_DEFAULT_TOP: i32 = 140;
const FLOATING_ASSIST_DEFAULT_SIZE: i32 = 82;
const FLOATING_ASSIST_TEMP_KEEP_SECS: u64 = 24 * 60 * 60;
const FLOATING_ASSIST_TEMP_CLEANUP_INTERVAL_MS: i64 = 6 * 60 * 60 * 1000;
const TRAY_TEMPLATE_ICON_SIZE: u32 = 64;
#[cfg(target_os = "macos")]
const DOCK_ICON_SCALE: f64 = 1.0;
#[cfg(target_os = "macos")]
const MACOS_APP_ICON_BYTES: &[u8] = include_bytes!("../icons/icon.icns");

#[cfg(target_os = "macos")]
static FLOATING_ASSIST_HOVER_OWNER_KEY: u8 = 0;
#[cfg(target_os = "macos")]
static FLOATING_ASSIST_HOVER_TRACKING_AREA_KEY: u8 = 0;

#[cfg(target_os = "macos")]
#[derive(Debug)]
struct FloatingAssistHoverOwnerIvars {
    window: tauri::WebviewWindow,
}

#[cfg(target_os = "macos")]
define_class!(
    #[unsafe(super(NSView))]
    #[name = "MemoryBreadFloatingAssistHoverOwner"]
    #[ivars = FloatingAssistHoverOwnerIvars]
    struct FloatingAssistHoverOwner;

    impl FloatingAssistHoverOwner {
        #[unsafe(method(mouseEntered:))]
        fn mouse_entered(&self, _event: &NSEvent) {
            let _ = self
                .ivars()
                .window
                .emit("floating-assist-native-hover-changed", true);
        }

        #[unsafe(method(mouseExited:))]
        fn mouse_exited(&self, _event: &NSEvent) {
            let _ = self
                .ivars()
                .window
                .emit("floating-assist-native-hover-changed", false);
        }
    }
);

#[cfg(target_os = "macos")]
fn configure_floating_assist_macos_window(window: &tauri::WebviewWindow) {
    let tracked_window = window.clone();
    let _ = window.run_on_main_thread(move || {
        let Ok(ns_window_ptr) = tracked_window.ns_window() else {
            return;
        };
        if ns_window_ptr.is_null() {
            return;
        }

        let ns_window: &NSWindow = unsafe { &*ns_window_ptr.cast() };
        ns_window.setIgnoresMouseEvents(false);
        ns_window.setAcceptsMouseMovedEvents(true);
        ns_window.setCollectionBehavior(
            ns_window.collectionBehavior()
                | NSWindowCollectionBehavior::CanJoinAllSpaces
                | NSWindowCollectionBehavior::Stationary
                | NSWindowCollectionBehavior::FullScreenAuxiliary,
        );

        let Some(content_view) = ns_window.contentView() else {
            return;
        };
        let view_ptr = Retained::as_ptr(&content_view).cast_mut().cast();
        let owner_key = std::ptr::addr_of!(FLOATING_ASSIST_HOVER_OWNER_KEY).cast();
        let mut owner_ptr = unsafe { objc_getAssociatedObject(view_ptr, owner_key) };
        if owner_ptr.is_null() {
            let Some(main_thread_marker) = MainThreadMarker::new() else {
                return;
            };
            let allocated_owner =
                main_thread_marker
                    .alloc()
                    .set_ivars(FloatingAssistHoverOwnerIvars {
                        window: tracked_window.clone(),
                    });
            let owner: Retained<FloatingAssistHoverOwner> =
                unsafe { msg_send![super(allocated_owner), initWithFrame: content_view.bounds()] };
            owner_ptr = Retained::as_ptr(&owner).cast::<AnyObject>();
            unsafe {
                objc_setAssociatedObject(
                    view_ptr,
                    owner_key,
                    owner_ptr.cast_mut(),
                    OBJC_ASSOCIATION_RETAIN_NONATOMIC,
                );
            }
        }

        let tracking_area_key = std::ptr::addr_of!(FLOATING_ASSIST_HOVER_TRACKING_AREA_KEY).cast();
        let previous_tracking_area_ptr =
            unsafe { objc_getAssociatedObject(view_ptr, tracking_area_key) };
        if !previous_tracking_area_ptr.is_null() {
            let previous_tracking_area: &NSTrackingArea =
                unsafe { &*previous_tracking_area_ptr.cast() };
            content_view.removeTrackingArea(previous_tracking_area);
        }

        let bounds = content_view.bounds();
        let mut ball_rect = bounds;
        ball_rect.origin.x = 8.0;
        ball_rect.origin.y = (bounds.size.height - 80.0).max(0.0);
        ball_rect.size.width = 72.0;
        ball_rect.size.height = 72.0;
        let options =
            NSTrackingAreaOptions::MouseEnteredAndExited | NSTrackingAreaOptions::ActiveAlways;
        let tracking_area = unsafe {
            NSTrackingArea::initWithRect_options_owner_userInfo(
                NSTrackingArea::alloc(),
                ball_rect,
                options,
                Some(&*owner_ptr),
                None,
            )
        };
        content_view.addTrackingArea(&tracking_area);

        unsafe {
            objc_setAssociatedObject(
                view_ptr,
                tracking_area_key,
                Retained::as_ptr(&tracking_area).cast_mut().cast(),
                OBJC_ASSOCIATION_RETAIN_NONATOMIC,
            );
        }
    });
}

#[cfg(not(target_os = "macos"))]
fn configure_floating_assist_macos_window(_window: &tauri::WebviewWindow) {}

struct TrayMenuState {
    capture: CheckMenuItem<tauri::Wry>,
    floating_assist: CheckMenuItem<tauri::Wry>,
    floating_assist_auto_task: CheckMenuItem<tauri::Wry>,
    autostart: CheckMenuItem<tauri::Wry>,
}

#[derive(Serialize)]
struct AppMetadata {
    product_name: String,
    version: String,
    build_number: String,
    platform: String,
    architecture: String,
    distribution: String,
    update_supported: bool,
}

#[cfg(not(feature = "app-store"))]
#[derive(Default)]
struct PendingSoftwareUpdate {
    update: Mutex<Option<Update>>,
}

#[cfg(not(feature = "app-store"))]
#[derive(Debug, Serialize)]
struct PreparedSoftwareUpdate {
    current_version: String,
    version: String,
    notes: Option<String>,
    published_at: Option<String>,
}

#[cfg(not(feature = "app-store"))]
#[derive(Debug, Clone, Serialize)]
struct SoftwareUpdateProgress {
    phase: &'static str,
    downloaded_bytes: u64,
    total_bytes: Option<u64>,
    percent: Option<u8>,
}

struct BundledBackendProcess {
    name: &'static str,
    child: Child,
}

#[derive(Default)]
struct BundledBackendState {
    children: Mutex<Vec<BundledBackendProcess>>,
}

#[derive(Default)]
struct FloatingAssistWindowState {
    expand_origin: Mutex<Option<PhysicalPosition<i32>>>,
}

#[derive(Default)]
struct PendingFloatingAssistActionState {
    action: Mutex<Option<String>>,
}

#[derive(Debug, Serialize)]
struct FloatingAssistOcrResult {
    text: String,
    confidence: f64,
    screenshot_path: String,
    width: u32,
    height: u32,
    screenshot_source: String,
    app_bundle_id: Option<String>,
    app_name: Option<String>,
    window_title: Option<String>,
}

#[derive(Debug, Serialize)]
struct FloatingAssistDragOrigin {
    offset_x: f64,
    offset_y: f64,
}

#[derive(Debug, Serialize)]
struct IpcRequest {
    id: String,
    ts: i64,
    task: IpcTask,
}

#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum IpcTask {
    Ocr {
        capture_id: i64,
        screenshot_path: String,
    },
}

#[derive(Debug, Deserialize)]
struct IpcResponse {
    status: String,
    result: Option<serde_json::Value>,
    error: Option<String>,
}

#[derive(Debug, Deserialize)]
struct IpcOcrResult {
    text: String,
    confidence: f64,
}

struct FloatingAssistScreenCapture {
    preview_path: PathBuf,
    ocr_paths: Vec<PathBuf>,
    width: u32,
    height: u32,
    source: String,
    app_bundle_id: Option<String>,
    app_name: Option<String>,
    window_title: Option<String>,
}

trait ReadWrite: Read + Write {}
impl<T: Read + Write> ReadWrite for T {}

#[cfg(target_os = "macos")]
fn configure_macos_dock_icon() {
    let Some(main_thread_marker) = MainThreadMarker::new() else {
        eprintln!("无法在主线程配置 macOS Dock 图标");
        return;
    };
    let application = NSApplication::sharedApplication(main_thread_marker);
    let icon_data = NSData::with_bytes(MACOS_APP_ICON_BYTES);
    let Some(application_icon) = NSImage::initWithData(NSImage::alloc(), &icon_data) else {
        eprintln!("无法从内置资源加载 macOS 应用图标");
        return;
    };
    unsafe {
        application.setApplicationIconImage(Some(&application_icon));
    }
    let dock_tile = application.dockTile();
    let tile_size = dock_tile.size();

    let container = NSView::initWithFrame(NSView::alloc(main_thread_marker), Default::default());
    container.setFrameSize(tile_size);

    let icon_view = NSImageView::imageViewWithImage(&application_icon, main_thread_marker);
    let mut icon_frame = icon_view.frame();
    icon_frame.size.width = tile_size.width * DOCK_ICON_SCALE;
    icon_frame.size.height = tile_size.height * DOCK_ICON_SCALE;
    icon_frame.origin.x = (tile_size.width - icon_frame.size.width) / 2.0;
    icon_frame.origin.y = (tile_size.height - icon_frame.size.height) / 2.0;
    icon_view.setFrame(icon_frame);

    container.addSubview(&icon_view);
    dock_tile.setContentView(Some(&container));
    dock_tile.display();
}

#[cfg(target_os = "macos")]
fn set_main_window_background_mode(app: &AppHandle, enabled: bool) {
    let policy = if enabled {
        tauri::ActivationPolicy::Accessory
    } else {
        tauri::ActivationPolicy::Regular
    };
    if let Err(error) = app.set_activation_policy(policy) {
        eprintln!("更新 macOS 应用显示模式失败: {error}");
    }
    if !enabled {
        if let Err(error) = app.run_on_main_thread(configure_macos_dock_icon) {
            eprintln!("配置 macOS Dock 图标失败: {error}");
        }
    }
}

#[cfg(not(target_os = "macos"))]
fn set_main_window_background_mode(_app: &AppHandle, _enabled: bool) {}

fn show_main_window(app: &AppHandle) {
    set_main_window_background_mode(app, false);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn ensure_floating_assist_window(app: &AppHandle) -> Result<tauri::WebviewWindow, String> {
    if let Some(window) = app.get_webview_window(FLOATING_ASSIST_LABEL) {
        configure_floating_assist_macos_window(&window);
        return Ok(window);
    }

    let window_builder = WebviewWindowBuilder::new(
        app,
        FLOATING_ASSIST_LABEL,
        WebviewUrl::App("index.html?view=floating-assist".into()),
    )
    .title("记忆面包悬浮球")
    .inner_size(
        FLOATING_ASSIST_DEFAULT_SIZE as f64,
        FLOATING_ASSIST_DEFAULT_SIZE as f64,
    )
    .min_inner_size(
        FLOATING_ASSIST_DEFAULT_SIZE as f64,
        FLOATING_ASSIST_DEFAULT_SIZE as f64,
    )
    .resizable(false)
    .decorations(false);
    #[cfg(not(feature = "app-store"))]
    let window_builder = window_builder.transparent(true);
    let window = window_builder
        .shadow(false)
        .always_on_top(true)
        .accept_first_mouse(true)
        .content_protected(false)
        .skip_taskbar(true)
        .visible(false)
        .position(960.0, 140.0)
        .build()
        .map_err(|error| error.to_string())?;
    configure_floating_assist_macos_window(&window);
    Ok(window)
}

fn default_floating_assist_position(
    app: &AppHandle,
    window: &tauri::WebviewWindow,
) -> PhysicalPosition<i32> {
    let window_size = window.outer_size().ok();
    let window_width = window_size
        .map(|size| size.width as i32)
        .unwrap_or(FLOATING_ASSIST_DEFAULT_SIZE);
    let window_height = window_size
        .map(|size| size.height as i32)
        .unwrap_or(FLOATING_ASSIST_DEFAULT_SIZE);
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| app.primary_monitor().ok().flatten());

    if let Some(monitor) = monitor {
        let monitor_position = monitor.position();
        let monitor_size = monitor.size();
        let min_x = monitor_position.x;
        let min_y = monitor_position.y;
        let max_x = min_x + monitor_size.width as i32 - window_width;
        let max_y = min_y + monitor_size.height as i32 - window_height;
        let x = max_x
            .saturating_sub(FLOATING_ASSIST_DEFAULT_MARGIN)
            .max(min_x);
        let y = (min_y + FLOATING_ASSIST_DEFAULT_TOP).clamp(min_y, max_y.max(min_y));
        return PhysicalPosition::new(x, y);
    }

    PhysicalPosition::new(960, FLOATING_ASSIST_DEFAULT_TOP)
}

fn monitor_contains_point(monitor: &Monitor, x: i32, y: i32) -> bool {
    let position = monitor.position();
    let size = monitor.size();
    x >= position.x
        && x < position.x + size.width as i32
        && y >= position.y
        && y < position.y + size.height as i32
}

fn monitor_for_position(
    app: &AppHandle,
    window: &tauri::WebviewWindow,
    position: PhysicalPosition<i32>,
) -> Option<Monitor> {
    app.available_monitors()
        .ok()
        .and_then(|monitors| {
            monitors
                .into_iter()
                .find(|monitor| monitor_contains_point(monitor, position.x, position.y))
        })
        .or_else(|| window.current_monitor().ok().flatten())
        .or_else(|| app.primary_monitor().ok().flatten())
}

fn clamp_position_to_monitor_work_area(
    monitor: &Monitor,
    position: PhysicalPosition<i32>,
    window_width: i32,
    window_height: i32,
) -> PhysicalPosition<i32> {
    let work_area = monitor.work_area();
    let min_x = work_area.position.x;
    let min_y = work_area.position.y;
    let max_x = min_x + work_area.size.width as i32 - window_width.max(1);
    let max_y = min_y + work_area.size.height as i32 - window_height.max(1);

    PhysicalPosition::new(
        position.x.clamp(min_x, max_x.max(min_x)),
        position.y.clamp(min_y, max_y.max(min_y)),
    )
}

fn floating_assist_outer_size(
    window: &tauri::WebviewWindow,
    target_logical_size: Option<(f64, f64)>,
) -> (i32, i32) {
    let target_size = target_logical_size.map(|(width, height)| {
        let scale_factor = window
            .current_monitor()
            .ok()
            .flatten()
            .map(|monitor| monitor.scale_factor())
            .unwrap_or(1.0);
        (
            (width * scale_factor).round() as i32,
            (height * scale_factor).round() as i32,
        )
    });

    if let Some(target_size) = target_size {
        return target_size;
    }

    if let Ok(size) = window.outer_size() {
        return (size.width as i32, size.height as i32);
    }

    (FLOATING_ASSIST_DEFAULT_SIZE, FLOATING_ASSIST_DEFAULT_SIZE)
}

fn set_floating_assist_position_clamped(
    app: &AppHandle,
    window: &tauri::WebviewWindow,
    position: PhysicalPosition<i32>,
    target_logical_size: Option<(f64, f64)>,
) -> Result<(), String> {
    let (window_width, window_height) = floating_assist_outer_size(window, target_logical_size);
    let clamped_position = monitor_for_position(app, window, position)
        .map(|monitor| {
            clamp_position_to_monitor_work_area(&monitor, position, window_width, window_height)
        })
        .unwrap_or(position);

    window
        .set_position(clamped_position)
        .map_err(|error| error.to_string())
}

fn floating_assist_is_collapsed_size(width: f64, height: f64) -> bool {
    width <= FLOATING_ASSIST_DEFAULT_SIZE as f64 && height <= FLOATING_ASSIST_DEFAULT_SIZE as f64
}

fn floating_assist_position_after_resize(
    expand_origin: &mut Option<PhysicalPosition<i32>>,
    current_position: PhysicalPosition<i32>,
    width: f64,
    height: f64,
) -> PhysicalPosition<i32> {
    if floating_assist_is_collapsed_size(width, height) {
        expand_origin.take().unwrap_or(current_position)
    } else {
        if expand_origin.is_none() {
            *expand_origin = Some(current_position);
        }
        current_position
    }
}

fn clear_floating_assist_expand_origin(app: &AppHandle) {
    if let Ok(mut expand_origin) = app
        .state::<FloatingAssistWindowState>()
        .expand_origin
        .lock()
    {
        *expand_origin = None;
    }
}

fn set_floating_assist_visible_inner(app: &AppHandle, enabled: bool) -> Result<(), String> {
    let menu_state = app.state::<TrayMenuState>();
    clear_floating_assist_expand_origin(app);
    if enabled {
        let window = ensure_floating_assist_window(app)?;
        let _ = window.set_size(LogicalSize::new(
            FLOATING_ASSIST_DEFAULT_SIZE as f64,
            FLOATING_ASSIST_DEFAULT_SIZE as f64,
        ));
        let _ = set_floating_assist_position_clamped(
            app,
            &window,
            default_floating_assist_position(app, &window),
            Some((
                FLOATING_ASSIST_DEFAULT_SIZE as f64,
                FLOATING_ASSIST_DEFAULT_SIZE as f64,
            )),
        );
        window.show().map_err(|error| error.to_string())?;
        let _ = window.set_content_protected(false);
        let _ = window.set_always_on_top(true);
        let _ = app.emit("floating-assist-reset", ());
        menu_state
            .floating_assist_auto_task
            .set_enabled(true)
            .map_err(|error| error.to_string())?;
    } else {
        // A hidden transparent WebView can keep a CoreAnimation/WindowServer layer alive.
        // Destroy it when the assist is disabled; it is recreated lazily on next enable.
        if let Some(window) = app.get_webview_window(FLOATING_ASSIST_LABEL) {
            window.destroy().map_err(|error| error.to_string())?;
        }
        menu_state
            .floating_assist_auto_task
            .set_checked(false)
            .map_err(|error| error.to_string())?;
        menu_state
            .floating_assist_auto_task
            .set_enabled(false)
            .map_err(|error| error.to_string())?;
        let _ = app.emit("floating-assist-auto-task-changed", false);
    }
    menu_state
        .floating_assist
        .set_checked(enabled)
        .map_err(|error| error.to_string())?;
    let _ = app.emit("tray-floating-assist-changed", enabled);
    Ok(())
}

fn floating_assist_temp_dir() -> Result<PathBuf, String> {
    let dir = memory_bread_home()?
        .join(".memory-bread")
        .join("floating-screenshots");
    fs::create_dir_all(&dir).map_err(|error| error.to_string())?;
    Ok(dir)
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64
}

fn cleanup_floating_assist_temp_files() -> Result<(usize, u64), String> {
    let dir = floating_assist_temp_dir()?;
    let now = SystemTime::now();
    let keep_duration = Duration::from_secs(FLOATING_ASSIST_TEMP_KEEP_SECS);
    let entries = fs::read_dir(&dir).map_err(|error| error.to_string())?;
    let mut deleted_count = 0usize;
    let mut freed_bytes = 0u64;

    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(_) => continue,
        };
        let metadata = match entry.metadata() {
            Ok(metadata) => metadata,
            Err(_) => continue,
        };
        if !metadata.is_file() {
            continue;
        }
        let Ok(modified) = metadata.modified() else {
            continue;
        };
        let Ok(age) = now.duration_since(modified) else {
            continue;
        };
        if age < keep_duration {
            continue;
        }

        let path = entry.path();
        let size = metadata.len();
        if fs::remove_file(&path).is_ok() {
            deleted_count += 1;
            freed_bytes += size;
        }
    }

    Ok((deleted_count, freed_bytes))
}

fn schedule_floating_assist_temp_cleanup(force: bool) {
    let now = now_ms();
    if force {
        LAST_FLOATING_ASSIST_TEMP_CLEANUP_MS.store(now, Ordering::Relaxed);
    } else {
        let last = LAST_FLOATING_ASSIST_TEMP_CLEANUP_MS.load(Ordering::Relaxed);
        if now.saturating_sub(last) < FLOATING_ASSIST_TEMP_CLEANUP_INTERVAL_MS {
            return;
        }
        if LAST_FLOATING_ASSIST_TEMP_CLEANUP_MS
            .compare_exchange(last, now, Ordering::Relaxed, Ordering::Relaxed)
            .is_err()
        {
            return;
        }
    }

    let _ = tauri::async_runtime::spawn_blocking(|| match cleanup_floating_assist_temp_files() {
        Ok((deleted_count, freed_bytes)) if deleted_count > 0 => {
            eprintln!("悬浮球临时截图清理完成: deleted={deleted_count}, freed_bytes={freed_bytes}");
        }
        Ok(_) => {}
        Err(error) => {
            eprintln!("悬浮球临时截图清理失败: {error}");
        }
    });
}

fn save_rgba_jpeg(path: PathBuf, image: RgbaImage) -> Result<PathBuf, String> {
    let rgb = DynamicImage::ImageRgba8(image).into_rgb8();
    let file = fs::File::create(&path).map_err(|error| error.to_string())?;
    let mut encoder = JpegEncoder::new_with_quality(file, 82);
    encoder
        .encode_image(&DynamicImage::ImageRgb8(rgb))
        .map_err(|error| error.to_string())?;
    Ok(path)
}

fn capture_all_screens_for_floating_assist() -> Result<FloatingAssistScreenCapture, String> {
    let monitors = xcap::Monitor::all()
        .map_err(|error| format!("无法读取屏幕列表，请确认已授予屏幕录制权限：{error}"))?;
    if monitors.is_empty() {
        return Err("未发现可用显示器".to_string());
    }

    let timestamp = now_ms();
    let temp_dir = floating_assist_temp_dir()?;
    let mut captures = Vec::with_capacity(monitors.len());
    let mut ocr_paths = Vec::with_capacity(monitors.len());
    for (index, monitor) in monitors.into_iter().enumerate() {
        let x = monitor
            .x()
            .map_err(|error| format!("读取显示器位置失败：{error}"))?;
        let y = monitor
            .y()
            .map_err(|error| format!("读取显示器位置失败：{error}"))?;
        let image = monitor
            .capture_image()
            .map_err(|error| format!("截图失败，请确认已授予屏幕录制权限：{error}"))?;
        let ocr_path = temp_dir.join(format!("{timestamp}-monitor-{index}.jpg"));
        ocr_paths.push(save_rgba_jpeg(ocr_path, image.clone())?);
        captures.push((x, y, image));
    }

    let min_x = captures
        .iter()
        .map(|(x, _, _)| *x as i64)
        .min()
        .unwrap_or(0);
    let min_y = captures
        .iter()
        .map(|(_, y, _)| *y as i64)
        .min()
        .unwrap_or(0);
    let max_x = captures
        .iter()
        .map(|(x, _, image)| *x as i64 + image.width() as i64)
        .max()
        .unwrap_or(0);
    let max_y = captures
        .iter()
        .map(|(_, y, image)| *y as i64 + image.height() as i64)
        .max()
        .unwrap_or(0);
    let width = u32::try_from(max_x - min_x).map_err(|_| "多显示器截图宽度过大".to_string())?;
    let height = u32::try_from(max_y - min_y).map_err(|_| "多显示器截图高度过大".to_string())?;
    let mut canvas = RgbaImage::from_pixel(width, height, Rgba([255, 252, 247, 255]));
    for (x, y, image) in captures {
        imageops::overlay(&mut canvas, &image, x as i64 - min_x, y as i64 - min_y);
    }

    let preview_path = save_rgba_jpeg(temp_dir.join(format!("{timestamp}.jpg")), canvas)?;
    Ok(FloatingAssistScreenCapture {
        preview_path,
        ocr_paths,
        width,
        height,
        source: "fullscreen".to_string(),
        app_bundle_id: None,
        app_name: None,
        window_title: None,
    })
}

#[cfg(target_os = "macos")]
fn frontmost_bundle_id_for_floating_assist() -> Option<String> {
    NSWorkspace::sharedWorkspace()
        .frontmostApplication()?
        .bundleIdentifier()
        .map(|identifier| identifier.to_string())
}

#[cfg(not(target_os = "macos"))]
fn frontmost_bundle_id_for_floating_assist() -> Option<String> {
    None
}

fn capture_focused_window_for_floating_assist() -> Result<FloatingAssistScreenCapture, String> {
    use xcap::Window;

    const MIN_WINDOW_WIDTH: u32 = 200;
    const MIN_WINDOW_HEIGHT: u32 = 150;

    let windows = Window::all().map_err(|error| format!("读取窗口列表失败：{error}"))?;
    let mut candidates: Vec<(Window, u32, u32, u64)> = Vec::new();
    for window in windows {
        if !window.is_focused().unwrap_or(false) || window.is_minimized().unwrap_or(false) {
            continue;
        }
        let width = window.width().unwrap_or(0);
        let height = window.height().unwrap_or(0);
        let area = (width as u64) * (height as u64);
        candidates.push((window, width, height, area));
    }

    candidates.sort_by(|left, right| right.3.cmp(&left.3));
    let (window, width, height, _) = candidates
        .into_iter()
        .next()
        .ok_or_else(|| "未找到前台窗口".to_string())?;

    let app_name = window
        .app_name()
        .ok()
        .filter(|value| !value.trim().is_empty());
    let app_bundle_id = frontmost_bundle_id_for_floating_assist();
    let window_title = window.title().ok().filter(|value| !value.trim().is_empty());
    if width < MIN_WINDOW_WIDTH || height < MIN_WINDOW_HEIGHT {
        return Err(format!(
            "前台窗口过小，跳过 app={app_name:?} title={window_title:?} {width}x{height}"
        ));
    }

    let image = window.capture_image().map_err(|error| {
        format!("前台窗口截图失败 app={app_name:?} title={window_title:?}：{error}")
    })?;
    let timestamp = now_ms();
    let temp_dir = floating_assist_temp_dir()?;
    let preview_path = save_rgba_jpeg(
        temp_dir.join(format!("{timestamp}-window.jpg")),
        image.clone(),
    )?;
    let ocr_path = save_rgba_jpeg(temp_dir.join(format!("{timestamp}-window-ocr.jpg")), image)?;

    Ok(FloatingAssistScreenCapture {
        preview_path,
        ocr_paths: vec![ocr_path],
        width,
        height,
        source: "window".to_string(),
        app_bundle_id,
        app_name,
        window_title,
    })
}

fn capture_screen_for_floating_assist() -> Result<FloatingAssistScreenCapture, String> {
    capture_focused_window_for_floating_assist()
        .or_else(|_| capture_all_screens_for_floating_assist())
}

fn run_floating_assist_ocr(paths: &[PathBuf]) -> Result<IpcOcrResult, String> {
    let mut parts = Vec::new();
    let mut confidence_sum = 0.0;
    let mut confidence_count = 0usize;
    let mut last_error = None;

    for (index, path) in paths.iter().enumerate() {
        let path_text = path.to_string_lossy().to_string();
        match send_sidecar_ocr(&path_text) {
            Ok(result) => {
                let text = result.text.trim();
                if !text.is_empty() {
                    parts.push(format!("显示器 {}:\n{}", index + 1, text));
                }
                confidence_sum += result.confidence;
                confidence_count += 1;
            }
            Err(error) => {
                last_error = Some(error);
            }
        }
    }

    if parts.is_empty() {
        if let Some(error) = last_error {
            return Err(error);
        }
    }

    Ok(IpcOcrResult {
        text: parts.join("\n\n"),
        confidence: if confidence_count > 0 {
            confidence_sum / confidence_count as f64
        } else {
            0.0
        },
    })
}

#[cfg(unix)]
fn ipc_socket_path() -> PathBuf {
    PACKAGED_IPC_SOCKET_PATH
        .get()
        .cloned()
        .or_else(|| {
            std::env::var_os(IPC_SOCKET_ENV)
                .filter(|value| !value.is_empty())
                .map(PathBuf::from)
        })
        .unwrap_or_else(|| PathBuf::from(DEFAULT_IPC_SOCKET_PATH))
}

#[cfg(unix)]
fn connect_ipc_stream() -> Result<Box<dyn ReadWrite>, String> {
    use std::os::unix::net::UnixStream;

    let socket_path = ipc_socket_path();
    let stream = UnixStream::connect(&socket_path)
        .map_err(|error| format!("无法连接 OCR sidecar（{}）：{error}", socket_path.display()))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(20)))
        .map_err(|error| error.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(20)))
        .map_err(|error| error.to_string())?;
    Ok(Box::new(stream))
}

#[cfg(windows)]
fn connect_ipc_stream() -> Result<Box<dyn ReadWrite>, String> {
    use std::net::TcpStream;

    let stream = TcpStream::connect("127.0.0.1:17071")
        .map_err(|error| format!("无法连接 OCR sidecar：{error}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(20)))
        .map_err(|error| error.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(20)))
        .map_err(|error| error.to_string())?;
    Ok(Box::new(stream))
}

#[cfg(not(any(unix, windows)))]
fn connect_ipc_stream() -> Result<Box<dyn ReadWrite>, String> {
    Err("当前平台暂不支持悬浮球 OCR IPC".to_string())
}

fn send_ipc_payload(payload: &[u8]) -> Result<IpcResponse, String> {
    let mut stream = connect_ipc_stream()?;
    let length = (payload.len() as u32).to_be_bytes();
    stream
        .write_all(&length)
        .map_err(|error| error.to_string())?;
    stream
        .write_all(payload)
        .map_err(|error| error.to_string())?;
    stream.flush().map_err(|error| error.to_string())?;

    let mut length_buf = [0u8; 4];
    stream
        .read_exact(&mut length_buf)
        .map_err(|error| error.to_string())?;
    let response_length = u32::from_be_bytes(length_buf) as usize;
    if response_length > 16 * 1024 * 1024 {
        return Err(format!("OCR sidecar 响应过大：{} 字节", response_length));
    }
    let mut response_buf = vec![0u8; response_length];
    stream
        .read_exact(&mut response_buf)
        .map_err(|error| error.to_string())?;
    serde_json::from_slice(&response_buf).map_err(|error| error.to_string())
}

fn send_sidecar_ocr(path: &str) -> Result<IpcOcrResult, String> {
    let request = IpcRequest {
        id: uuid::Uuid::new_v4().to_string(),
        ts: now_ms(),
        task: IpcTask::Ocr {
            capture_id: 0,
            screenshot_path: path.to_string(),
        },
    };
    let payload = serde_json::to_vec(&request).map_err(|error| error.to_string())?;
    let response = send_ipc_payload(&payload)?;
    if response.status != "ok" {
        return Err(response
            .error
            .unwrap_or_else(|| "OCR sidecar 返回未知错误".to_string()));
    }
    let result = response
        .result
        .ok_or_else(|| "OCR sidecar 响应缺少 result".to_string())?;
    serde_json::from_value(result).map_err(|error| error.to_string())
}

fn memory_bread_home() -> Result<PathBuf, String> {
    if let Some(path) = PACKAGED_RUNTIME_HOME.get() {
        return Ok(path.clone());
    }
    std::env::var("HOME")
        .map(PathBuf::from)
        .map_err(|_| "无法定位用户目录".to_string())
}

#[cfg(not(debug_assertions))]
fn bundled_helper_path(name: &str) -> Result<PathBuf, String> {
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let directory = executable
        .parent()
        .ok_or_else(|| "无法定位 App 内置服务目录".to_string())?;
    let helper = if name == "memory-bread-ai" {
        directory
            .parent()
            .ok_or_else(|| "无法定位 App Contents 目录".to_string())?
            .join("Helpers")
            .join("memory-bread-ai.app")
            .join("Contents")
            .join("MacOS")
            .join(name)
    } else {
        directory.join(name)
    };
    if !helper.is_file() {
        return Err(format!("App 缺少内置服务: {}", helper.display()));
    }
    Ok(helper)
}

#[cfg(not(debug_assertions))]
fn spawn_bundled_backend(
    name: &'static str,
    executable: &PathBuf,
    args: &[&str],
    runtime_home: &PathBuf,
    log_dir: &PathBuf,
    ipc_socket_path: &PathBuf,
) -> Result<BundledBackendProcess, String> {
    let log_path = log_dir.join(format!("{name}.log"));
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|error| format!("无法打开 {} 日志: {error}", log_path.display()))?;
    let stderr = log.try_clone().map_err(|error| error.to_string())?;
    let working_directory = executable
        .parent()
        .ok_or_else(|| format!("无法定位 {name} 工作目录"))?;
    let child = Command::new(executable)
        .args(args)
        .current_dir(working_directory)
        .env("HOME", runtime_home)
        .env("MEMORY_BREAD_PACKAGED", "1")
        .env(IPC_SOCKET_ENV, ipc_socket_path)
        .env("PYTHONUNBUFFERED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(stderr))
        .spawn()
        .map_err(|error| format!("启动内置服务 {name} 失败: {error}"))?;
    Ok(BundledBackendProcess { name, child })
}

#[cfg(not(debug_assertions))]
fn start_bundled_backends(app: &AppHandle) -> Result<(), String> {
    let state = app.state::<BundledBackendState>();
    let mut children = state
        .children
        .lock()
        .map_err(|_| "内置服务状态锁已损坏".to_string())?;
    if !children.is_empty() {
        return Ok(());
    }

    let runtime_home = app
        .path()
        .app_data_dir()
        .map_err(|error| error.to_string())?
        .join("runtime");
    let log_dir = runtime_home.join(".memory-bread").join("logs");
    fs::create_dir_all(&log_dir).map_err(|error| error.to_string())?;
    let _ = PACKAGED_RUNTIME_HOME.set(runtime_home.clone());
    #[cfg(unix)]
    let ipc_socket_path =
        std::env::temp_dir().join(format!("memory-bread-sidecar-{}.sock", std::process::id()));
    #[cfg(not(unix))]
    let ipc_socket_path = PathBuf::new();
    #[cfg(unix)]
    let _ = PACKAGED_IPC_SOCKET_PATH.set(ipc_socket_path.clone());

    let ai = bundled_helper_path("memory-bread-ai")?;
    let core = bundled_helper_path("memory-bread-core")?;
    let services: [(&'static str, &PathBuf, &[&str]); 4] = [
        ("sidecar", &ai, &["sidecar"]),
        ("model_api", &ai, &["model-api"]),
        ("creation", &ai, &["creation"]),
        ("core", &core, &[]),
    ];

    let mut started = Vec::with_capacity(services.len());
    for (name, executable, args) in services {
        match spawn_bundled_backend(
            name,
            executable,
            args,
            &runtime_home,
            &log_dir,
            &ipc_socket_path,
        ) {
            Ok(child) => started.push(child),
            Err(error) => {
                for process in started.iter_mut().rev() {
                    let _ = process.child.kill();
                    let _ = process.child.wait();
                }
                return Err(error);
            }
        }
    }
    *children = started;
    Ok(())
}

fn stop_bundled_backends(app: &AppHandle) {
    let state = app.state::<BundledBackendState>();
    let Ok(mut children) = state.children.lock() else {
        return;
    };
    for process in children.iter_mut().rev() {
        if process.child.try_wait().ok().flatten().is_none() {
            if let Err(error) = process.child.kill() {
                eprintln!("停止内置服务 {} 失败: {error}", process.name);
            }
        }
        let _ = process.child.wait();
    }
    children.clear();
}

#[cfg(debug_assertions)]
fn start_script_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .join("start.sh")
}

#[cfg(debug_assertions)]
fn supervisor_shutdown_in_progress() -> bool {
    let Ok(home) = memory_bread_home() else {
        return false;
    };
    let marker = home
        .join(".memory-bread")
        .join("state")
        .join(SUPERVISOR_SHUTDOWN_MARKER);
    let Ok(metadata) = marker.metadata() else {
        return false;
    };

    // 标记只在 start.sh stop/restart 的几秒内有效；异常遗留的旧文件不能永久
    // 禁用用户退出时的后端清理。
    metadata
        .modified()
        .ok()
        .and_then(|modified| modified.elapsed().ok())
        .map(|age| age < Duration::from_secs(60))
        .unwrap_or(false)
}

/// 退出 App 后再由独立脚本停止启动器和所有后台服务，避免脚本先杀掉当前进程。
#[cfg(debug_assertions)]
fn schedule_full_shutdown() {
    if supervisor_shutdown_in_progress() {
        return;
    }
    if FULL_SHUTDOWN_SCHEDULED.swap(true, Ordering::SeqCst) {
        return;
    }
    let script = start_script_path();
    if !script.is_file() {
        eprintln!("未找到全组件停止脚本: {}", script.display());
        return;
    }

    let result = Command::new("/bin/bash")
        .arg(script)
        .arg("stop-after-app")
        .arg(std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();

    if let Err(error) = result {
        eprintln!("启动全组件停止脚本失败: {error}");
    }
}

#[cfg(not(debug_assertions))]
fn schedule_full_shutdown() {}

#[cfg(debug_assertions)]
fn schedule_backend_startup() {
    let script = start_script_path();
    if !script.is_file() {
        eprintln!("未找到全组件启动脚本: {}", script.display());
        return;
    }

    if let Err(error) = Command::new("/bin/bash")
        .arg(script)
        .arg("start-backends")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        eprintln!("启动后台服务失败: {error}");
    }
}

#[cfg(not(debug_assertions))]
fn schedule_backend_startup() {}

#[cfg(not(feature = "app-store"))]
fn autostart_enabled(app: &AppHandle) -> bool {
    app.autolaunch().is_enabled().unwrap_or(false)
}

#[cfg(feature = "app-store")]
fn autostart_enabled(_app: &AppHandle) -> bool {
    false
}

#[cfg(not(feature = "app-store"))]
fn update_autostart(app: &AppHandle, enabled: bool) -> Result<(), String> {
    if enabled {
        app.autolaunch().enable().map_err(|error| error.to_string())
    } else {
        app.autolaunch()
            .disable()
            .map_err(|error| error.to_string())
    }
}

#[cfg(feature = "app-store")]
fn update_autostart(_app: &AppHandle, _enabled: bool) -> Result<(), String> {
    Err("Mac App Store 版本暂不支持登录时自动启动".to_string())
}

#[tauri::command]
fn set_capture_menu_state(app: AppHandle, enabled: bool) -> Result<(), String> {
    app.state::<TrayMenuState>()
        .capture
        .set_checked(enabled)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn get_app_metadata(app: AppHandle) -> AppMetadata {
    AppMetadata {
        product_name: "记忆面包".to_string(),
        version: app.package_info().version.to_string(),
        build_number: option_env!("MEMORY_BREAD_BUILD_NUMBER")
            .unwrap_or("1")
            .to_string(),
        platform: std::env::consts::OS.to_string(),
        architecture: std::env::consts::ARCH.to_string(),
        distribution: if cfg!(feature = "app-store") {
            "app_store".to_string()
        } else {
            "direct".to_string()
        },
        update_supported: !cfg!(feature = "app-store")
            && option_env!("MEMORY_BREAD_UPDATER_PUBLIC_KEY")
                .map(str::trim)
                .is_some_and(|value| !value.is_empty()),
    }
}

#[cfg(not(feature = "app-store"))]
#[tauri::command]
async fn prepare_software_update(
    app: AppHandle,
    state: tauri::State<'_, PendingSoftwareUpdate>,
    manifest_url: String,
    environment: String,
    cohort: String,
) -> Result<Option<PreparedSoftwareUpdate>, String> {
    if option_env!("MEMORY_BREAD_UPDATER_PUBLIC_KEY")
        .map(str::trim)
        .is_none_or(str::is_empty)
    {
        return Err("此安装包未配置更新签名公钥，请重新安装正式发布包".to_string());
    }
    if !matches!(environment.as_str(), "production" | "staging") {
        return Err("软件更新环境不合法".to_string());
    }
    if cohort.len() != 64
        || !cohort
            .chars()
            .all(|character| character.is_ascii_hexdigit())
    {
        return Err("软件更新 cohort 不合法".to_string());
    }
    let endpoint: tauri::Url = manifest_url
        .parse()
        .map_err(|_| "软件更新清单地址不合法".to_string())?;
    if !cfg!(debug_assertions) && endpoint.scheme() != "https" {
        return Err("正式版只允许使用 HTTPS 更新清单".to_string());
    }

    let updater = app
        .updater_builder()
        .endpoints(vec![endpoint])
        .map_err(|error| format!("软件更新地址不可用：{error}"))?
        .header("X-MemoryBread-Environment", environment)
        .map_err(|error| format!("软件更新环境头无效：{error}"))?
        .header("X-MemoryBread-Update-Cohort", cohort)
        .map_err(|error| format!("软件更新 cohort 无效：{error}"))?
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|error| format!("无法初始化软件更新器：{error}"))?;
    let update = updater
        .check()
        .await
        .map_err(|error| format!("检查签名更新包失败：{error}"))?;
    let Some(update) = update else {
        *state.update.lock().map_err(|_| "软件更新状态不可用")? = None;
        return Ok(None);
    };
    let prepared = PreparedSoftwareUpdate {
        current_version: update.current_version.clone(),
        version: update.version.clone(),
        notes: update.body.clone(),
        published_at: update.date.map(|date| date.to_string()),
    };
    *state.update.lock().map_err(|_| "软件更新状态不可用")? = Some(update);
    Ok(Some(prepared))
}

#[cfg(not(feature = "app-store"))]
#[tauri::command]
async fn download_and_install_software_update(
    app: AppHandle,
    state: tauri::State<'_, PendingSoftwareUpdate>,
) -> Result<(), String> {
    let update = state
        .update
        .lock()
        .map_err(|_| "软件更新状态不可用")?
        .take()
        .ok_or_else(|| "请先重新检查可安装的更新".to_string())?;
    let retry_update = update.clone();
    let expected_checksum = update
        .raw_json
        .get("checksum_sha256")
        .and_then(serde_json::Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_lowercase)
        .ok_or_else(|| "更新清单缺少 SHA-256，已拒绝安装".to_string())?;

    let downloaded = Arc::new(AtomicU64::new(0));
    let progress_downloaded = Arc::clone(&downloaded);
    let progress_app = app.clone();
    let finish_app = app.clone();
    let _ = app.emit(
        "software-update-progress",
        SoftwareUpdateProgress {
            phase: "downloading",
            downloaded_bytes: 0,
            total_bytes: update
                .raw_json
                .get("download_size_bytes")
                .and_then(serde_json::Value::as_u64),
            percent: Some(0),
        },
    );
    let bytes = match update
        .download(
            move |chunk_length, content_length| {
                let current = progress_downloaded.fetch_add(chunk_length as u64, Ordering::SeqCst)
                    + chunk_length as u64;
                let percent = content_length
                    .filter(|total| *total > 0)
                    .map(|total| ((current.saturating_mul(100) / total).min(100)) as u8);
                let _ = progress_app.emit(
                    "software-update-progress",
                    SoftwareUpdateProgress {
                        phase: "downloading",
                        downloaded_bytes: current,
                        total_bytes: content_length,
                        percent,
                    },
                );
            },
            move || {
                let _ = finish_app.emit(
                    "software-update-progress",
                    SoftwareUpdateProgress {
                        phase: "verifying",
                        downloaded_bytes: downloaded.load(Ordering::SeqCst),
                        total_bytes: None,
                        percent: Some(100),
                    },
                );
            },
        )
        .await
    {
        Ok(bytes) => bytes,
        Err(error) => {
            *state.update.lock().map_err(|_| "软件更新状态不可用")? = Some(retry_update);
            return Err(format!("更新包下载或签名校验失败：{error}"));
        }
    };

    let actual_checksum = format!("{:x}", Sha256::digest(&bytes));
    if actual_checksum != expected_checksum {
        *state.update.lock().map_err(|_| "软件更新状态不可用")? = Some(retry_update);
        return Err("更新包 SHA-256 校验失败，已拒绝安装".to_string());
    }
    let _ = app.emit(
        "software-update-progress",
        SoftwareUpdateProgress {
            phase: "installing",
            downloaded_bytes: bytes.len() as u64,
            total_bytes: Some(bytes.len() as u64),
            percent: Some(100),
        },
    );
    if let Err(error) = update.install(bytes) {
        *state.update.lock().map_err(|_| "软件更新状态不可用")? = Some(retry_update);
        return Err(format!("更新包安装失败：{error}"));
    }
    let _ = app.emit(
        "software-update-progress",
        SoftwareUpdateProgress {
            phase: "ready_to_restart",
            downloaded_bytes: 0,
            total_bytes: None,
            percent: Some(100),
        },
    );
    Ok(())
}

#[tauri::command]
fn restart_application(app: AppHandle) -> Result<(), String> {
    QUITTING.store(true, Ordering::SeqCst);
    stop_bundled_backends(&app);
    app.restart()
}

#[tauri::command]
#[allow(deprecated)]
fn open_external_url(app: AppHandle, url: String) -> Result<(), String> {
    let url = url.trim();
    if !url.starts_with("https://") || url.len() > 2_000 || url.chars().any(char::is_whitespace) {
        return Err("只能打开有效的 HTTPS 下载地址".to_string());
    }
    app.shell()
        .open(url.to_string(), None)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn set_floating_assist_menu_state(app: AppHandle, enabled: bool) -> Result<(), String> {
    let menu_state = app.state::<TrayMenuState>();
    menu_state
        .floating_assist
        .set_checked(enabled)
        .map_err(|error| error.to_string())?;
    menu_state
        .floating_assist_auto_task
        .set_enabled(enabled)
        .map_err(|error| error.to_string())?;
    if !enabled {
        menu_state
            .floating_assist_auto_task
            .set_checked(false)
            .map_err(|error| error.to_string())?;
        let _ = app.emit("floating-assist-auto-task-changed", false);
    }
    Ok(())
}

#[tauri::command]
fn set_floating_assist_auto_task_menu_state(
    app: AppHandle,
    checked: bool,
    enabled: bool,
) -> Result<(), String> {
    let menu_state = app.state::<TrayMenuState>();
    menu_state
        .floating_assist_auto_task
        .set_enabled(enabled)
        .map_err(|error| error.to_string())?;
    menu_state
        .floating_assist_auto_task
        .set_checked(checked && enabled)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn set_floating_assist_visible(app: AppHandle, enabled: bool) -> Result<(), String> {
    set_floating_assist_visible_inner(&app, enabled)
}

#[tauri::command]
fn show_main_panel_from_floating_assist(app: AppHandle) -> Result<(), String> {
    show_main_window(&app);
    Ok(())
}

#[tauri::command]
fn trigger_floating_assist_action(app: AppHandle, action: String) -> Result<(), String> {
    if action != "recognize_screen_task" {
        return Err(format!("不支持的悬浮球动作: {action}"));
    }

    {
        let state = app.state::<PendingFloatingAssistActionState>();
        let mut pending = state.action.lock().map_err(|error| error.to_string())?;
        *pending = Some(action.clone());
    }

    if let Err(error) = set_floating_assist_visible_inner(&app, true) {
        if let Ok(mut pending) = app
            .state::<PendingFloatingAssistActionState>()
            .action
            .lock()
        {
            *pending = None;
        }
        return Err(error);
    }
    app.emit("floating-assist-action-triggered", action)
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn take_pending_floating_assist_action(app: AppHandle) -> Result<Option<String>, String> {
    let state = app.state::<PendingFloatingAssistActionState>();
    let mut pending = state.action.lock().map_err(|error| error.to_string())?;
    Ok(pending.take())
}

#[tauri::command]
fn set_floating_assist_position(app: AppHandle, x: f64, y: f64) -> Result<(), String> {
    let window = ensure_floating_assist_window(&app)?;
    set_floating_assist_position_clamped(
        &app,
        &window,
        PhysicalPosition::new(x.round() as i32, y.round() as i32),
        None,
    )
}

#[tauri::command]
fn begin_floating_assist_drag(app: AppHandle) -> Result<FloatingAssistDragOrigin, String> {
    let window = ensure_floating_assist_window(&app)?;
    let window_position = window.outer_position().map_err(|error| error.to_string())?;
    let cursor_position = app.cursor_position().map_err(|error| error.to_string())?;
    Ok(FloatingAssistDragOrigin {
        offset_x: cursor_position.x - f64::from(window_position.x),
        offset_y: cursor_position.y - f64::from(window_position.y),
    })
}

#[tauri::command]
fn update_floating_assist_drag(app: AppHandle, offset_x: f64, offset_y: f64) -> Result<(), String> {
    let window = ensure_floating_assist_window(&app)?;
    let cursor_position = app.cursor_position().map_err(|error| error.to_string())?;
    set_floating_assist_position_clamped(
        &app,
        &window,
        PhysicalPosition::new(
            (cursor_position.x - offset_x).round() as i32,
            (cursor_position.y - offset_y).round() as i32,
        ),
        None,
    )
}

#[tauri::command]
fn set_floating_assist_size(app: AppHandle, width: f64, height: f64) -> Result<(), String> {
    let window = ensure_floating_assist_window(&app)?;
    let current_position = window.outer_position().map_err(|error| error.to_string())?;
    let window_state = app.state::<FloatingAssistWindowState>();
    let mut expand_origin = window_state
        .expand_origin
        .lock()
        .map_err(|error| error.to_string())?;
    window
        .set_size(LogicalSize::new(width, height))
        .map_err(|error| error.to_string())?;

    let target_position =
        floating_assist_position_after_resize(&mut expand_origin, current_position, width, height);
    set_floating_assist_position_clamped(&app, &window, target_position, Some((width, height)))?;
    configure_floating_assist_macos_window(&window);

    Ok(())
}

#[tauri::command]
fn read_floating_assist_image_data_url(path: String) -> Result<String, String> {
    let bytes = fs::read(&path).map_err(|error| format!("读取截屏失败：{error}"))?;
    let encoded = general_purpose::STANDARD.encode(bytes);
    Ok(format!("data:image/jpeg;base64,{encoded}"))
}

#[tauri::command]
fn open_floating_assist_reference(app: AppHandle, detail: serde_json::Value) -> Result<(), String> {
    show_main_window(&app);
    app.emit("floating-assist-open-reference", detail)
        .map_err(|error| error.to_string())
}

#[tauri::command]
#[allow(deprecated)]
fn open_export_folder(app: AppHandle, path: String) -> Result<(), String> {
    let exported_path =
        fs::canonicalize(&path).map_err(|error| format!("找不到已导出的记忆包：{error}"))?;
    let folder = if exported_path.is_dir() {
        exported_path
    } else {
        exported_path
            .parent()
            .map(PathBuf::from)
            .ok_or_else(|| "无法确定备份所在文件夹".to_string())?
    };

    app.shell()
        .open(folder.to_string_lossy().into_owned(), None)
        .map_err(|error| format!("无法打开备份所在文件夹：{error}"))
}

#[tauri::command]
fn pick_local_directory() -> Option<String> {
    rfd::FileDialog::new()
        .set_title("选择 Obsidian Vault 文件夹")
        .pick_folder()
        .map(|path| path.to_string_lossy().into_owned())
}

#[tauri::command]
async fn capture_screen_ocr_for_floating_assist(
    app: AppHandle,
) -> Result<FloatingAssistOcrResult, String> {
    schedule_floating_assist_temp_cleanup(false);

    let floating_window = app.get_webview_window(FLOATING_ASSIST_LABEL);
    if let Some(window) = floating_window.as_ref() {
        let _ = window.set_content_protected(true);
        let _ = window.set_always_on_top(true);

        let capture_result = tauri::async_runtime::spawn_blocking(|| {
            let capture = capture_screen_for_floating_assist()?;
            let result = run_floating_assist_ocr(&capture.ocr_paths)?;
            Ok::<_, String>((capture, result))
        })
        .await
        .map_err(|error| format!("悬浮球截图任务失败：{error}"));

        let _ = window.set_content_protected(false);
        let _ = window.show();
        let _ = window.set_always_on_top(true);

        let (capture, result) = capture_result??;
        let screenshot_path = capture.preview_path.to_string_lossy().to_string();
        return Ok(FloatingAssistOcrResult {
            text: result.text,
            confidence: result.confidence,
            screenshot_path,
            width: capture.width,
            height: capture.height,
            screenshot_source: capture.source,
            app_bundle_id: capture.app_bundle_id,
            app_name: capture.app_name,
            window_title: capture.window_title,
        });
    }

    let (capture, result) = tauri::async_runtime::spawn_blocking(|| {
        let capture = capture_screen_for_floating_assist()?;
        let result = run_floating_assist_ocr(&capture.ocr_paths)?;
        Ok::<_, String>((capture, result))
    })
    .await
    .map_err(|error| format!("悬浮球截图任务失败：{error}"))??;
    let screenshot_path = capture.preview_path.to_string_lossy().to_string();
    Ok(FloatingAssistOcrResult {
        text: result.text,
        confidence: result.confidence,
        screenshot_path,
        width: capture.width,
        height: capture.height,
        screenshot_source: capture.source,
        app_bundle_id: capture.app_bundle_id,
        app_name: capture.app_name,
        window_title: capture.window_title,
    })
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default().plugin(tauri_plugin_shell::init());
    #[cfg(not(feature = "app-store"))]
    let builder = builder.plugin(
        tauri_plugin_updater::Builder::new()
            .pubkey(option_env!("MEMORY_BREAD_UPDATER_PUBLIC_KEY").unwrap_or(""))
            .build(),
    );
    #[cfg(desktop)]
    let builder = builder.plugin(tauri_plugin_global_shortcut::Builder::new().build());
    #[cfg(not(feature = "app-store"))]
    let builder = builder.plugin(tauri_plugin_autostart::init(
        MacosLauncher::LaunchAgent,
        Some(vec!["--autostart"]),
    ));
    let builder = builder
        .manage(BundledBackendState::default())
        .manage(FloatingAssistWindowState::default())
        .manage(PendingFloatingAssistActionState::default());
    #[cfg(not(feature = "app-store"))]
    let builder = builder.manage(PendingSoftwareUpdate::default());
    builder
        .invoke_handler(tauri::generate_handler![
            get_app_metadata,
            restart_application,
            #[cfg(not(feature = "app-store"))]
            prepare_software_update,
            #[cfg(not(feature = "app-store"))]
            download_and_install_software_update,
            open_external_url,
            set_capture_menu_state,
            set_floating_assist_menu_state,
            set_floating_assist_auto_task_menu_state,
            set_floating_assist_visible,
            show_main_panel_from_floating_assist,
            trigger_floating_assist_action,
            take_pending_floating_assist_action,
            set_floating_assist_position,
            begin_floating_assist_drag,
            update_floating_assist_drag,
            set_floating_assist_size,
            read_floating_assist_image_data_url,
            open_floating_assist_reference,
            open_export_folder,
            pick_local_directory,
            capture_screen_ocr_for_floating_assist,
        ])
        .setup(|app| {
            let started_in_background = std::env::args().any(|argument| argument == "--autostart");
            set_main_window_background_mode(app.handle(), started_in_background);

            #[cfg(not(debug_assertions))]
            if let Err(error) = start_bundled_backends(app.handle()) {
                eprintln!("内置服务启动失败: {error}");
            }

            let main_panel = MenuItem::with_id(app, "main-panel", "主面板", true, None::<&str>)?;
            let capture =
                CheckMenuItem::with_id(app, "capture", "打开/关闭采集", true, false, None::<&str>)?;
            let floating_assist = CheckMenuItem::with_id(
                app,
                "floating-assist",
                "打开/关闭悬浮球",
                true,
                false,
                None::<&str>,
            )?;
            let floating_assist_auto_task = CheckMenuItem::with_id(
                app,
                "floating-assist-auto-task",
                "自动识别任务",
                false,
                false,
                None::<&str>,
            )?;
            let autostart_enabled = autostart_enabled(app.handle());
            let autostart = CheckMenuItem::with_id(
                app,
                "autostart",
                "开机默认启动",
                !cfg!(feature = "app-store"),
                autostart_enabled,
                None::<&str>,
            )?;
            let settings = MenuItem::with_id(app, "settings", "设置", true, None::<&str>)?;
            let about = MenuItem::with_id(app, "about", "关于记忆面包", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(
                app,
                &[
                    &main_panel,
                    &capture,
                    &floating_assist,
                    &floating_assist_auto_task,
                    &autostart,
                    &settings,
                    &about,
                    &separator,
                    &quit,
                ],
            )?;

            app.manage(TrayMenuState {
                capture: capture.clone(),
                floating_assist: floating_assist.clone(),
                floating_assist_auto_task: floating_assist_auto_task.clone(),
                autostart: autostart.clone(),
            });

            schedule_floating_assist_temp_cleanup(true);

            let mut tray = TrayIconBuilder::with_id("memory-bread")
                .menu(&menu)
                .show_menu_on_left_click(true)
                .tooltip("记忆面包")
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "main-panel" => show_main_window(app),
                    "capture" => {
                        let enabled = app
                            .state::<TrayMenuState>()
                            .capture
                            .is_checked()
                            .unwrap_or(true);
                        let _ = app.emit("tray-capture-changed", enabled);
                    }
                    "floating-assist" => {
                        let enabled = app
                            .state::<TrayMenuState>()
                            .floating_assist
                            .is_checked()
                            .unwrap_or(false);
                        if let Err(error) = set_floating_assist_visible_inner(app, enabled) {
                            eprintln!("更新悬浮球状态失败: {error}");
                            let _ = app
                                .state::<TrayMenuState>()
                                .floating_assist
                                .set_checked(!enabled);
                        }
                    }
                    "floating-assist-auto-task" => {
                        let state = app.state::<TrayMenuState>();
                        let floating_enabled = state.floating_assist.is_checked().unwrap_or(false);
                        let requested = state
                            .floating_assist_auto_task
                            .is_checked()
                            .unwrap_or(false);
                        if !floating_enabled {
                            let _ = state.floating_assist_auto_task.set_checked(false);
                            let _ = state.floating_assist_auto_task.set_enabled(false);
                            let _ = app.emit("floating-assist-auto-task-changed", false);
                        } else {
                            let _ = app.emit("floating-assist-auto-task-changed", requested);
                        }
                    }
                    "autostart" => {
                        let state = app.state::<TrayMenuState>();
                        let requested = state.autostart.is_checked().unwrap_or(false);
                        let result = update_autostart(app, requested);
                        if let Err(error) = result {
                            eprintln!("更新开机启动状态失败: {error}");
                            let _ = state.autostart.set_checked(!requested);
                        }
                    }
                    "settings" => {
                        show_main_window(app);
                        let _ = app.emit("tray-navigate-settings", ());
                    }
                    "about" => {
                        show_main_window(app);
                        let _ = app.emit("tray-navigate-about", ());
                    }
                    "quit" => {
                        QUITTING.store(true, Ordering::SeqCst);
                        stop_bundled_backends(app);
                        schedule_full_shutdown();
                        app.exit(0);
                    }
                    _ => {}
                });

            #[cfg(target_os = "macos")]
            {
                let tray_icon = tauri::image::Image::new(
                    include_bytes!("../icons/tray-template.rgba"),
                    TRAY_TEMPLATE_ICON_SIZE,
                    TRAY_TEMPLATE_ICON_SIZE,
                );
                tray = tray.icon(tray_icon).icon_as_template(true);
            }
            #[cfg(not(target_os = "macos"))]
            {
                if let Some(icon) = app.default_window_icon() {
                    tray = tray.icon(icon.clone());
                }
            }
            tray.build(app)?;

            if started_in_background {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
            }

            // 开发窗口可能由 `tauri dev` 直接启动，也会在 Rust 源码变化后独立重启。
            // 每次 setup 都执行幂等的后端启动检查，避免只剩 UI 时初始化接口报 Load failed。
            schedule_backend_startup();

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                if !QUITTING.load(Ordering::SeqCst) {
                    api.prevent_close();
                    let _ = window.hide();
                    if window.label() == "main" {
                        set_main_window_background_mode(window.app_handle(), true);
                    }
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            #[cfg(target_os = "macos")]
            if matches!(&event, RunEvent::Ready) {
                // Ready 时显式注册并刷新内置图标，避免直接运行 target/debug
                // 二进制时回退为 macOS 的 exec 通用图标。
                if let Err(error) = app.run_on_main_thread(configure_macos_dock_icon) {
                    eprintln!("刷新 macOS Dock 图标失败: {error}");
                }
            }
            #[cfg(target_os = "macos")]
            if matches!(&event, RunEvent::Reopen { .. }) {
                show_main_window(app);
            }
            if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
                QUITTING.store(true, Ordering::SeqCst);
                stop_bundled_backends(app);
                schedule_full_shutdown();
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floating_assist_collapse_restores_the_expand_origin() {
        let collapsed_position = PhysicalPosition::new(1814, 140);
        let expanded_position = PhysicalPosition::new(1504, 140);
        let mut expand_origin = None;

        let expand_target = floating_assist_position_after_resize(
            &mut expand_origin,
            collapsed_position,
            392.0,
            502.0,
        );
        assert_eq!(expand_target, collapsed_position);
        assert_eq!(expand_origin, Some(collapsed_position));

        let resized_target = floating_assist_position_after_resize(
            &mut expand_origin,
            expanded_position,
            720.0,
            540.0,
        );
        assert_eq!(resized_target, expanded_position);
        assert_eq!(expand_origin, Some(collapsed_position));

        let collapse_target = floating_assist_position_after_resize(
            &mut expand_origin,
            expanded_position,
            FLOATING_ASSIST_DEFAULT_SIZE as f64,
            FLOATING_ASSIST_DEFAULT_SIZE as f64,
        );
        assert_eq!(collapse_target, collapsed_position);
        assert_eq!(expand_origin, None);
    }

    #[test]
    fn floating_assist_collapsed_resize_keeps_the_current_position_without_an_origin() {
        let current_position = PhysicalPosition::new(320, 240);
        let mut expand_origin = None;

        let target = floating_assist_position_after_resize(
            &mut expand_origin,
            current_position,
            FLOATING_ASSIST_DEFAULT_SIZE as f64,
            FLOATING_ASSIST_DEFAULT_SIZE as f64,
        );

        assert_eq!(target, current_position);
        assert_eq!(expand_origin, None);
    }
}
