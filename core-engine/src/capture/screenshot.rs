//! 截图采集与 JPEG 压缩存储
//!
//! 生产环境：使用 `xcap` crate 采集全屏截图，
//! 转换为 JPEG（质量可配置），存储到时间戳命名的文件。
//!
//! 测试环境：`capture_and_save` 返回 None，不调用系统 API。

#[cfg(test)]
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
#[cfg(test)]
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use image::{imageops::FilterType, DynamicImage};

use super::CaptureError;

// ─────────────────────────────────────────────────────────────────────────────
// 截图熔断器（防止显卡驱动崩溃时持续重试）
// ─────────────────────────────────────────────────────────────────────────────

static SCREENSHOT_FAILURE_COUNT: AtomicU32 = AtomicU32::new(0);
static LAST_FAILURE_RESET: AtomicU64 = AtomicU64::new(0);
const MAX_CONSECUTIVE_FAILURES: u32 = 3;
const FAILURE_RESET_WINDOW_SECS: u64 = 60;

/// 检查截图熔断器状态
fn check_screenshot_circuit_breaker() -> bool {
    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    let failure_count = SCREENSHOT_FAILURE_COUNT.load(Ordering::Relaxed);
    let last_reset = LAST_FAILURE_RESET.load(Ordering::Relaxed);

    // 超过重置窗口，重置计数器
    if now_secs - last_reset > FAILURE_RESET_WINDOW_SECS {
        SCREENSHOT_FAILURE_COUNT.store(0, Ordering::Relaxed);
        LAST_FAILURE_RESET.store(now_secs, Ordering::Relaxed);
        return true;
    }

    // 检查是否超过阈值
    if failure_count >= MAX_CONSECUTIVE_FAILURES {
        tracing::error!(
            failure_count,
            "截图熔断：连续失败 {} 次，暂停截图功能",
            failure_count
        );
        return false;
    }

    true
}

/// 记录截图失败
fn record_screenshot_failure() {
    let count = SCREENSHOT_FAILURE_COUNT.fetch_add(1, Ordering::Relaxed) + 1;
    tracing::warn!("截图失败计数: {}/{}", count, MAX_CONSECUTIVE_FAILURES);
}

/// 重置截图失败计数
fn reset_screenshot_failure() {
    SCREENSHOT_FAILURE_COUNT.store(0, Ordering::Relaxed);
}

// ─────────────────────────────────────────────────────────────────────────────
// 公共类型
// ─────────────────────────────────────────────────────────────────────────────

/// 截图保存结果
#[derive(Debug, Clone)]
pub struct ScreenshotResult {
    /// 相对于 captures_dir 的路径（写入数据库 screenshot_path 字段）
    pub relative_path: String,
    /// 截图文件的完整磁盘路径
    pub full_path: PathBuf,
    /// 感知哈希（dHash）用于近似去重
    pub dhash: u64,
    /// 图像宽度（像素）
    pub width: u32,
    /// 图像高度（像素）
    pub height: u32,
    /// JPEG 文件大小（字节）
    pub file_size: u64,
    /// 截图来源：`window`（前台窗口截图）或 `fullscreen`（全屏回退）
    pub source: ScreenshotSource,
    /// 前台窗口所属应用名。仅窗口截图可用，用于在 AX 标题缺失时补齐采集上下文。
    pub app_name: Option<String>,
    /// 前台窗口标题。仅窗口截图可用，用于在 AX 标题缺失时补齐采集上下文。
    pub window_title: Option<String>,
}

/// 只存在于内存中的低成本画面探针。获取像素后立即计算分块指纹；只有门禁判定为
/// 明显变化时，才会把这里的同一帧编码为 JPEG，避免二次截图和画面竞态。
#[derive(Debug)]
pub(crate) struct VisualProbe {
    image: DynamicImage,
    pub fingerprint: VisualFingerprint,
    pub dhash: u64,
    pub source: ScreenshotSource,
    pub app_name: Option<String>,
    pub window_title: Option<String>,
}

/// 4×4 分块、每块 64 bit dHash。相比单个全局 dHash，小范围文本或局部滚动更容易被识别。
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct VisualFingerprint {
    tiles: [u64; 16],
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct VisualDifference {
    pub total_bits: u32,
    pub max_tile_bits: u32,
    pub changed_tiles: u32,
}

impl VisualFingerprint {
    pub fn difference(&self, other: &Self) -> VisualDifference {
        let mut total_bits = 0;
        let mut max_tile_bits = 0;
        let mut changed_tiles = 0;
        for (left, right) in self.tiles.iter().zip(other.tiles.iter()) {
            let bits = hamming_distance(*left, *right);
            total_bits += bits;
            max_tile_bits = max_tile_bits.max(bits);
            if bits > 0 {
                changed_tiles += 1;
            }
        }
        VisualDifference {
            total_bits,
            max_tile_bits,
            changed_tiles,
        }
    }

    /// 允许单块轻微光标/动画噪声；局部明显变化或多块累计变化才进入完整采集。
    pub fn is_significant_change(&self, other: &Self) -> bool {
        let difference = self.difference(other);
        difference.max_tile_bits >= 5
            || difference.total_bits >= 12
            || (difference.changed_tiles >= 3 && difference.total_bits >= 8)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ScreenshotSource {
    Window,
    Fullscreen,
}

impl ScreenshotSource {
    pub fn as_str(self) -> &'static str {
        match self {
            ScreenshotSource::Window => "window",
            ScreenshotSource::Fullscreen => "fullscreen",
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 公共 API
// ─────────────────────────────────────────────────────────────────────────────

/// 采集主显示器截图并以 JPEG 格式存储（异步版本）。
///
/// 返回 `Ok(None)` 表示无可用显示器（无头服务器 / 测试环境）或熔断保护。
pub async fn capture_and_save_async(
    captures_dir: PathBuf,
    quality: u8,
) -> Result<Option<ScreenshotResult>, CaptureError> {
    // 使用 spawn_blocking 避免阻塞 tokio runtime
    tokio::task::spawn_blocking(move || capture_and_save(&captures_dir, quality))
        .await
        .map_err(|e| CaptureError::ScreenshotFailed(format!("截图任务 panic: {}", e)))?
}

/// 只抓取前台窗口像素并计算视觉指纹，不编码、不落盘、不触发 OCR。
pub(crate) async fn capture_visual_probe_async() -> Result<Option<VisualProbe>, CaptureError> {
    tokio::task::spawn_blocking(capture_visual_probe)
        .await
        .map_err(|error| CaptureError::ScreenshotFailed(format!("视觉探针任务 panic: {error}")))?
}

/// 将已经通过指纹门禁的内存帧落盘，不再次调用系统截图 API。
pub(crate) async fn save_visual_probe_async(
    probe: VisualProbe,
    captures_dir: PathBuf,
    quality: u8,
) -> Result<ScreenshotResult, CaptureError> {
    tokio::task::spawn_blocking(move || save_visual_probe(probe, &captures_dir, quality))
        .await
        .map_err(|error| CaptureError::ScreenshotFailed(format!("视觉帧保存任务 panic: {error}")))?
}

/// 采集主显示器截图并以 JPEG 格式存储。
///
/// 返回 `Ok(None)` 表示无可用显示器（无头服务器 / 测试环境）或熔断保护。
pub fn capture_and_save(
    captures_dir: &Path,
    quality: u8,
) -> Result<Option<ScreenshotResult>, CaptureError> {
    let Some(probe) = capture_visual_probe()? else {
        return Ok(None);
    };
    save_visual_probe(probe, captures_dir, quality).map(Some)
}

/// 生成截图文件的相对路径。
///
/// 格式：`screenshots/{timestamp_ms}.jpg`
pub fn make_relative_path(ts_ms: i64) -> String {
    format!("screenshots/{}.jpg", ts_ms)
}

/// 计算图像的 64-bit dHash（difference hash）。
pub fn compute_dhash64(image: &DynamicImage) -> u64 {
    let resized = image
        .resize_exact(9, 8, FilterType::Triangle)
        .grayscale()
        .to_luma8();

    let mut hash = 0u64;
    for y in 0..8 {
        for x in 0..8 {
            let left = resized.get_pixel(x, y)[0];
            let right = resized.get_pixel(x + 1, y)[0];
            hash <<= 1;
            if left > right {
                hash |= 1;
            }
        }
    }

    hash
}

/// 计算两个 dHash 的汉明距离。
pub fn hamming_distance(a: u64, b: u64) -> u32 {
    (a ^ b).count_ones()
}

fn compute_visual_fingerprint(image: &DynamicImage) -> VisualFingerprint {
    const GRID: u32 = 4;
    const TILE_WIDTH: u32 = 9;
    const TILE_HEIGHT: u32 = 8;
    // 整帧只缩放一次到 36×32 灰度图，再从中读取 16 个 tile；避免对原始大图做
    // 16 次裁剪和缩放。最终仅保留 128 字节指纹。
    let sampled = image
        .resize_exact(GRID * TILE_WIDTH, GRID * TILE_HEIGHT, FilterType::Triangle)
        .grayscale()
        .to_luma8();
    let mut tiles = [0u64; 16];
    for tile_y in 0..GRID {
        for tile_x in 0..GRID {
            let mut hash = 0u64;
            let x0 = tile_x * TILE_WIDTH;
            let y0 = tile_y * TILE_HEIGHT;
            for y in 0..TILE_HEIGHT {
                for x in 0..(TILE_WIDTH - 1) {
                    hash <<= 1;
                    if sampled.get_pixel(x0 + x, y0 + y)[0]
                        > sampled.get_pixel(x0 + x + 1, y0 + y)[0]
                    {
                        hash |= 1;
                    }
                }
            }
            tiles[(tile_y * GRID + tile_x) as usize] = hash;
        }
    }
    VisualFingerprint { tiles }
}

fn capture_visual_probe() -> Result<Option<VisualProbe>, CaptureError> {
    if !check_screenshot_circuit_breaker() {
        return Ok(None);
    }

    #[cfg(not(test))]
    let result = capture_real_probe();
    #[cfg(test)]
    let result = capture_test_probe();

    match result {
        Ok(probe) => {
            reset_screenshot_failure();
            Ok(probe)
        }
        Err(error) => {
            record_screenshot_failure();
            Err(error)
        }
    }
}

fn save_visual_probe(
    probe: VisualProbe,
    captures_dir: &Path,
    quality: u8,
) -> Result<ScreenshotResult, CaptureError> {
    use image::codecs::jpeg::JpegEncoder;
    use std::fs;
    use std::io::BufWriter;
    use std::time::{SystemTime, UNIX_EPOCH};

    let ts_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as i64;
    let relative_path = make_relative_path(ts_ms);
    let full_path = captures_dir.join(&relative_path);
    if let Some(parent) = full_path.parent() {
        fs::create_dir_all(parent)?;
    }

    let width = probe.image.width();
    let height = probe.image.height();
    let rgb_image = probe.image.into_rgb8();
    let file = fs::File::create(&full_path)?;
    let writer = BufWriter::new(file);
    let mut encoder = JpegEncoder::new_with_quality(writer, quality);
    encoder
        .encode_image(&DynamicImage::ImageRgb8(rgb_image))
        .map_err(|error| CaptureError::ImageError(error.to_string()))?;
    drop(encoder);
    let file_size = fs::metadata(&full_path)?.len();

    Ok(ScreenshotResult {
        relative_path,
        full_path,
        dhash: probe.dhash,
        width,
        height,
        file_size,
        source: probe.source,
        app_name: probe.app_name,
        window_title: probe.window_title,
    })
}

#[cfg(test)]
#[derive(Debug, Clone)]
struct TestScreenshotFixture {
    width: u32,
    height: u32,
    pixels: Vec<u8>,
}

#[cfg(test)]
fn test_screenshot_queue() -> &'static Mutex<VecDeque<TestScreenshotFixture>> {
    static TEST_SCREENSHOT_QUEUE: OnceLock<Mutex<VecDeque<TestScreenshotFixture>>> =
        OnceLock::new();
    TEST_SCREENSHOT_QUEUE.get_or_init(|| Mutex::new(VecDeque::new()))
}

#[cfg(test)]
pub(crate) fn clear_test_screenshots() {
    if let Ok(mut guard) = test_screenshot_queue().lock() {
        guard.clear();
    }
}

#[cfg(test)]
pub(crate) fn push_test_screenshot(width: u32, height: u32, pixels: Vec<u8>) {
    let fixture = TestScreenshotFixture {
        width,
        height,
        pixels,
    };
    test_screenshot_queue().lock().unwrap().push_back(fixture);
}

#[cfg(test)]
pub(crate) fn push_test_screenshot_from_image(image: &DynamicImage) {
    let rgb = image.to_rgb8();
    push_test_screenshot(rgb.width(), rgb.height(), rgb.into_raw());
}

// ─────────────────────────────────────────────────────────────────────────────
// 真实截图实现（仅非测试编译）
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(not(test))]
fn pick_active_monitor() -> Result<xcap::Monitor, CaptureError> {
    use enigo::{Enigo, Mouse, Settings};
    use xcap::Monitor;

    // 1) 尝试拿鼠标全局坐标，再用 from_point 选出鼠标所在屏。
    //    Enigo::new 在权限缺失等情况下可能返回 Err；我们都视为"取不到"，回落主屏。
    let mouse_xy = Enigo::new(&Settings::default())
        .ok()
        .and_then(|e| e.location().ok());

    if let Some((x, y)) = mouse_xy {
        match Monitor::from_point(x, y) {
            Ok(m) => {
                tracing::debug!(x, y, "pick_active_monitor: 命中鼠标所在屏");
                return Ok(m);
            }
            Err(e) => {
                tracing::warn!(x, y, error = %e, "from_point 失败，回落主屏");
            }
        }
    } else {
        tracing::debug!("无法获取鼠标坐标，回落主屏");
    }

    // 2) 回落：找 is_primary 主屏；再退一步取第一块。
    let monitors = Monitor::all().map_err(|e| {
        tracing::error!("获取显示器列表失败（可能是显卡驱动问题）: {}", e);
        std::thread::sleep(std::time::Duration::from_secs(1));
        CaptureError::ScreenshotFailed(e.to_string())
    })?;

    if monitors.is_empty() {
        return Err(CaptureError::ScreenshotFailed("无可用显示器".into()));
    }

    let primary = monitors
        .iter()
        .find(|m| m.is_primary().unwrap_or(false))
        .cloned();

    Ok(primary.unwrap_or_else(|| monitors.into_iter().next().unwrap()))
}

#[cfg(not(test))]
struct FocusedWindowCapture {
    image: image::RgbaImage,
    app_name: Option<String>,
    window_title: Option<String>,
}

#[cfg(not(test))]
fn non_empty_string(value: String) -> Option<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

#[cfg(not(test))]
fn capture_focused_window_image() -> Result<FocusedWindowCapture, String> {
    use xcap::Window;

    let windows = Window::all().map_err(|e| format!("Window::all 失败: {e}"))?;
    if windows.is_empty() {
        return Err("Window::all 返回空列表".into());
    }

    // 注意：xcap 在 macOS 上的 is_focused() 是「应用级」判断（只比较进程 PID），
    // 而非「窗口级」。当前台应用弹出下拉框 / popup / 输入法候选框时，这些都是
    // 属于同一进程的独立窗口，is_focused() 同样返回 true。因此不能取第一个就 break，
    // 否则可能选中那个面积很小的弹层而漏掉真正的软件主框体。
    // 策略：收集所有 focused 且未最小化的窗口，选面积最大的作为主框体。
    const MIN_WINDOW_WIDTH: u32 = 200;
    const MIN_WINDOW_HEIGHT: u32 = 150;

    let mut focused_check_errors: Vec<String> = Vec::new();
    let mut candidates: Vec<(Window, u32, u32, u64)> = Vec::new();
    for w in windows {
        match w.is_focused() {
            Ok(true) => {
                if w.is_minimized().unwrap_or(false) {
                    continue;
                }
                let width = w.width().unwrap_or(0);
                let height = w.height().unwrap_or(0);
                let area = (width as u64) * (height as u64);
                candidates.push((w, width, height, area));
            }
            Ok(false) => {}
            Err(e) => focused_check_errors.push(format!("is_focused err: {e}")),
        }
    }

    if candidates.is_empty() {
        return Err(if focused_check_errors.is_empty() {
            "无 focused 窗口（可能处于 Mission Control / 桌面 / 菜单栏弹层 / 窗口已最小化）"
                .to_string()
        } else {
            format!(
                "无 focused 窗口；is_focused 调用错误 {} 次：{}",
                focused_check_errors.len(),
                focused_check_errors.join("; ")
            )
        });
    }

    let candidate_count = candidates.len();
    // 同一前台应用可能有多个 focused 窗口（主窗体 + 下拉框等弹层），取面积最大的主框体。
    candidates.sort_by(|a, b| b.3.cmp(&a.3));
    let (win, width, height, _area) = candidates.into_iter().next().unwrap();

    let app_name = win.app_name().unwrap_or_default();
    let win_title = win.title().unwrap_or_default();

    if candidate_count > 1 {
        tracing::debug!(
            app = %app_name,
            title = %win_title,
            candidate_count,
            chosen = format!("{width}x{height}"),
            "同一前台应用存在多个 focused 窗口，已选面积最大的主框体（排除下拉框 / 弹层）"
        );
    }

    if width < MIN_WINDOW_WIDTH || height < MIN_WINDOW_HEIGHT {
        return Err(format!(
            "focused 窗口过小，跳过 app={app_name:?} title={win_title:?} {width}x{height}"
        ));
    }

    let image = win.capture_image().map_err(|e| {
        format!("Window::capture_image 失败 app={app_name:?} title={win_title:?}: {e}")
    })?;

    Ok(FocusedWindowCapture {
        image,
        app_name: non_empty_string(app_name),
        window_title: non_empty_string(win_title),
    })
}

#[cfg(not(test))]
fn capture_fullscreen_image() -> Result<image::RgbaImage, CaptureError> {
    let monitor = pick_active_monitor()?;
    monitor.capture_image().map_err(|e| {
        tracing::warn!("活动屏截图失败: {}", e);
        CaptureError::ScreenshotFailed(e.to_string())
    })
}

#[cfg(not(test))]
fn capture_real_probe() -> Result<Option<VisualProbe>, CaptureError> {
    let (rgba_image, source, app_name, window_title) = match capture_focused_window_image() {
        Ok(capture) => (
            capture.image,
            ScreenshotSource::Window,
            capture.app_name,
            capture.window_title,
        ),
        Err(reason) => {
            tracing::warn!(
                fallback_reason = %reason,
                "前台窗口截图失败，回退到全屏截图（OCR 可能扫到非目标窗口内容）"
            );
            match capture_fullscreen_image() {
                Ok(img) => (img, ScreenshotSource::Fullscreen, None, None),
                Err(e) => return Err(e),
            }
        }
    };
    let dynamic = DynamicImage::ImageRgba8(rgba_image);
    let dhash = compute_dhash64(&dynamic);
    let fingerprint = compute_visual_fingerprint(&dynamic);
    Ok(Some(VisualProbe {
        image: dynamic,
        fingerprint,
        dhash,
        source,
        app_name,
        window_title,
    }))
}

#[cfg(test)]
fn capture_test_probe() -> Result<Option<VisualProbe>, CaptureError> {
    use image::RgbImage;
    let fixture = match test_screenshot_queue().lock().unwrap().pop_front() {
        Some(fixture) => fixture,
        None => return Ok(None),
    };

    let TestScreenshotFixture {
        width,
        height,
        pixels,
    } = fixture;
    let rgb_image = RgbImage::from_raw(width, height, pixels)
        .ok_or_else(|| CaptureError::ImageError("invalid test screenshot pixels".to_string()))?;
    let dynamic = DynamicImage::ImageRgb8(rgb_image);
    let dhash = compute_dhash64(&dynamic);
    let fingerprint = compute_visual_fingerprint(&dynamic);
    Ok(Some(VisualProbe {
        image: dynamic,
        fingerprint,
        dhash,
        source: ScreenshotSource::Fullscreen,
        app_name: None,
        window_title: None,
    }))
}

// ─────────────────────────────────────────────────────────────────────────────
// 测试
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use image::{DynamicImage, GrayImage, Luma, RgbImage};
    use tempfile::tempdir;

    #[test]
    fn test_capture_returns_none_in_test_env() {
        clear_test_screenshots();
        let dir = tempdir().unwrap();
        let result = capture_and_save(dir.path(), 80).unwrap();
        assert!(result.is_none(), "测试环境未注入截图时不应产生截图");
    }

    #[test]
    fn test_make_relative_path_format() {
        let path = make_relative_path(1_700_000_000_000);
        assert!(path.starts_with("screenshots/"), "应以 screenshots/ 开头");
        assert!(path.ends_with(".jpg"), "应以 .jpg 结尾");
        assert!(path.contains("1700000000000"), "应包含时间戳");
    }

    #[test]
    fn test_make_relative_path_unique() {
        // 不同时间戳应生成不同路径
        let p1 = make_relative_path(1_000_000_000);
        let p2 = make_relative_path(1_000_000_001);
        assert_ne!(p1, p2);
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

    #[test]
    fn test_compute_dhash64_same_image_same_hash() {
        let image = gradient_image(0);
        let hash1 = compute_dhash64(&image);
        let hash2 = compute_dhash64(&image);
        assert_eq!(hash1, hash2);
    }

    #[test]
    fn tiled_visual_fingerprint_detects_local_content_change() {
        let mut baseline = GrayImage::new(128, 128);
        for y in 0..128 {
            for x in 0..128 {
                baseline.put_pixel(x, y, Luma([x as u8 ^ (y as u8 / 3)]));
            }
        }
        let mut changed = baseline.clone();
        for y in 32..64 {
            for x in 64..96 {
                changed.put_pixel(x, y, Luma([255u8.saturating_sub(x as u8)]));
            }
        }

        let baseline = compute_visual_fingerprint(&DynamicImage::ImageLuma8(baseline));
        let identical = baseline.clone();
        let changed = compute_visual_fingerprint(&DynamicImage::ImageLuma8(changed));
        assert!(!baseline.is_significant_change(&identical));
        assert!(baseline.is_significant_change(&changed));
        assert!(baseline.difference(&changed).changed_tiles >= 1);
    }

    #[test]
    fn test_hamming_distance_counts_bit_differences() {
        let hash = 0b1011u64;
        assert_eq!(hamming_distance(hash, hash), 0);
        assert_eq!(hamming_distance(hash, hash ^ 0b1), 1);
        assert_eq!(hamming_distance(hash, hash ^ 0b11), 2);
    }

    #[test]
    fn test_capture_test_returns_fixture_with_dhash() {
        clear_test_screenshots();
        let dir = tempdir().unwrap();
        let image = DynamicImage::ImageRgb8(RgbImage::from_fn(16, 16, |x, y| {
            image::Rgb([(x * 3) as u8, (y * 5) as u8, (x + y) as u8])
        }));
        let expected_hash = compute_dhash64(&image);
        push_test_screenshot_from_image(&image);

        let result = capture_and_save(dir.path(), 80).unwrap().unwrap();
        assert_eq!(result.dhash, expected_hash);
        assert!(result.full_path.exists());
        assert!(result.file_size > 0);
    }

    /// 验证 image crate 的 JPEG 编码流程（不依赖系统截图 API）
    #[test]
    fn test_jpeg_encode_from_raw_pixels() {
        use image::codecs::jpeg::JpegEncoder;
        use image::{DynamicImage, RgbImage};
        use std::io::Cursor;

        // 创建 8×8 纯色 RGB 图像
        let width = 8u32;
        let height = 8u32;
        let pixels: Vec<u8> = (0..width * height * 3)
            .map(|i| match i % 3 {
                0 => 200, // R
                1 => 100, // G
                _ => 50,  // B
            })
            .collect();

        let rgb_image = RgbImage::from_raw(width, height, pixels).unwrap();
        let dynamic = DynamicImage::ImageRgb8(rgb_image);

        let mut buf = Cursor::new(Vec::<u8>::new());
        let mut encoder = JpegEncoder::new_with_quality(&mut buf, 80);
        encoder.encode_image(&dynamic).expect("JPEG 编码应成功");

        let bytes = buf.into_inner();
        assert!(!bytes.is_empty(), "JPEG 字节流不应为空");
        // JPEG 文件以 FF D8 开头
        assert_eq!(bytes[0], 0xFF, "JPEG 魔数第1字节");
        assert_eq!(bytes[1], 0xD8, "JPEG 魔数第2字节");
    }

    /// 验证 JPEG 文件可以被 image crate 重新解码
    #[test]
    fn test_jpeg_roundtrip() {
        use image::codecs::jpeg::JpegEncoder;
        use image::{DynamicImage, RgbImage};
        use std::io::Cursor;

        let width = 4u32;
        let height = 4u32;
        let pixels: Vec<u8> = vec![128u8; (width * height * 3) as usize];

        let rgb = RgbImage::from_raw(width, height, pixels).unwrap();
        let dyn_ = DynamicImage::ImageRgb8(rgb);

        let mut encoded = Cursor::new(Vec::<u8>::new());
        JpegEncoder::new_with_quality(&mut encoded, 90)
            .encode_image(&dyn_)
            .unwrap();

        // 重新解码
        encoded.set_position(0);
        let decoded = image::load(encoded, image::ImageFormat::Jpeg).unwrap();
        // JPEG 有损，尺寸应保持一致
        assert_eq!(decoded.width(), width);
        assert_eq!(decoded.height(), height);
    }

    #[test]
    fn visual_probe_saves_rgba_window_frame_as_jpeg() {
        let dir = tempdir().unwrap();
        let image = DynamicImage::ImageRgba8(image::RgbaImage::from_pixel(
            32,
            24,
            image::Rgba([12, 34, 56, 180]),
        ));
        let probe = VisualProbe {
            fingerprint: compute_visual_fingerprint(&image),
            dhash: compute_dhash64(&image),
            image,
            source: ScreenshotSource::Window,
            app_name: Some("Test".into()),
            window_title: Some("RGBA".into()),
        };

        let saved = save_visual_probe(probe, dir.path(), 80).unwrap();
        assert!(saved.full_path.exists());
        let decoded = image::open(saved.full_path).unwrap();
        assert_eq!((decoded.width(), decoded.height()), (32, 24));
    }
}
