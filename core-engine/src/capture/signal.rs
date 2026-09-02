//! 用户交互信号监听与聚合。
//!
//! 键盘、点击、滚动只用于判断“内容可能已变化”。键盘回调不会读取键码、字符、
//! 修饰键或输入内容；生产链路中也不会构造或保存原始按键数据。

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::mpsc;
use tracing::{info, warn};

use super::CaptureEvent;

const SIGNAL_QUIET_SECS: u64 = 3;
const SIGNAL_MAX_WAIT_SECS: u64 = 5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum InputSignal {
    KeyboardActivity,
    MouseClick,
    Scroll,
}

impl InputSignal {
    fn into_capture_event(self) -> CaptureEvent {
        match self {
            Self::KeyboardActivity => CaptureEvent::KeyPause,
            Self::MouseClick => CaptureEvent::MouseClick { x: 0.0, y: 0.0 },
            Self::Scroll => CaptureEvent::Scroll,
        }
    }
}

#[derive(Debug)]
struct PendingSignals {
    first_at: Instant,
    last_at: Instant,
    strongest: InputSignal,
}

impl PendingSignals {
    fn new(signal: InputSignal, now: Instant) -> Self {
        Self {
            first_at: now,
            last_at: now,
            strongest: signal,
        }
    }

    fn push(&mut self, signal: InputSignal, now: Instant) {
        self.last_at = now;
        if signal_priority(signal) > signal_priority(self.strongest) {
            self.strongest = signal;
        }
    }

    fn deadline(&self) -> Instant {
        let quiet_deadline = self.last_at + Duration::from_secs(SIGNAL_QUIET_SECS);
        let max_deadline = self.first_at + Duration::from_secs(SIGNAL_MAX_WAIT_SECS);
        quiet_deadline.min(max_deadline)
    }
}

fn signal_priority(signal: InputSignal) -> u8 {
    match signal {
        InputSignal::KeyboardActivity => 3,
        InputSignal::Scroll => 2,
        InputSignal::MouseClick => 1,
    }
}

/// 把高频输入事件聚合为 3 秒停顿或最长 5 秒一个的采集信号。
/// 应用切换和 URL 变化属于明确信号，立即透传，并清掉同一批待处理输入事件。
pub async fn start_signal_aggregator(
    mut rx: mpsc::Receiver<CaptureEvent>,
    tx: mpsc::Sender<CaptureEvent>,
) {
    let mut pending: Option<PendingSignals> = None;

    loop {
        let deadline = pending.as_ref().map(PendingSignals::deadline);
        tokio::select! {
            maybe_event = rx.recv() => {
                let Some(event) = maybe_event else { break };
                if let Some(signal) = input_signal_from_event(&event) {
                    let now = Instant::now();
                    match pending.as_mut() {
                        Some(batch) => batch.push(signal, now),
                        None => pending = Some(PendingSignals::new(signal, now)),
                    }
                    continue;
                }

                pending = None;
                if tx.send(event).await.is_err() {
                    break;
                }
            }
            _ = async {
                if let Some(deadline) = deadline {
                    tokio::time::sleep_until(tokio::time::Instant::from_std(deadline)).await;
                }
            }, if deadline.is_some() => {
                let Some(batch) = pending.take() else { continue };
                if tx.send(batch.strongest.into_capture_event()).await.is_err() {
                    break;
                }
            }
        }
    }
}

fn input_signal_from_event(event: &CaptureEvent) -> Option<InputSignal> {
    match event {
        CaptureEvent::KeyPause => Some(InputSignal::KeyboardActivity),
        CaptureEvent::MouseClick { .. } => Some(InputSignal::MouseClick),
        CaptureEvent::Scroll => Some(InputSignal::Scroll),
        _ => None,
    }
}

/// 启动只读系统输入事件监听。监听失败时保留应用/URL 变化和定时兜底链路。
pub fn start_input_signal_listener(
    capture_enabled: Arc<AtomicBool>,
    keyboard_enabled: Arc<AtomicBool>,
    tx: mpsc::Sender<CaptureEvent>,
) {
    #[cfg(target_os = "macos")]
    {
        std::thread::Builder::new()
            .name("mb-input-signals".to_string())
            .spawn(move || {
                if let Err(reason) = run_macos_event_tap(capture_enabled, keyboard_enabled, tx) {
                    warn!(%reason, "输入信号监听不可用，继续使用应用/URL 变化与定时兜底");
                }
            })
            .map(|_| info!("已启动只读键盘停顿、点击和滚动信号监听"))
            .unwrap_or_else(|error| warn!(%error, "输入信号监听线程启动失败"));
    }

    #[cfg(not(target_os = "macos"))]
    {
        let _ = (capture_enabled, keyboard_enabled, tx);
        info!("当前平台暂不启用系统输入信号监听");
    }
}

#[cfg(target_os = "macos")]
fn run_macos_event_tap(
    capture_enabled: Arc<AtomicBool>,
    keyboard_enabled: Arc<AtomicBool>,
    tx: mpsc::Sender<CaptureEvent>,
) -> Result<(), String> {
    use core_foundation::runloop::{kCFRunLoopCommonModes, CFRunLoop};
    use core_graphics::event::{
        CGEventTap, CGEventTapLocation, CGEventTapOptions, CGEventTapPlacement, CGEventType,
    };

    let current = CFRunLoop::get_current();
    let tap = CGEventTap::new(
        CGEventTapLocation::Session,
        CGEventTapPlacement::TailAppendEventTap,
        CGEventTapOptions::ListenOnly,
        vec![
            CGEventType::KeyDown,
            CGEventType::LeftMouseDown,
            CGEventType::RightMouseDown,
            CGEventType::OtherMouseDown,
            CGEventType::ScrollWheel,
        ],
        move |_proxy, event_type, event_ref| {
            if !capture_enabled.load(Ordering::Relaxed) {
                return None;
            }
            let event = match event_type {
                CGEventType::KeyDown if keyboard_enabled.load(Ordering::Relaxed) => {
                    Some(CaptureEvent::KeyPause)
                }
                CGEventType::LeftMouseDown
                | CGEventType::RightMouseDown
                | CGEventType::OtherMouseDown => {
                    let location = event_ref.location();
                    Some(CaptureEvent::MouseClick {
                        x: location.x,
                        y: location.y,
                    })
                }
                CGEventType::ScrollWheel => Some(CaptureEvent::Scroll),
                _ => None,
            };
            if let Some(event) = event {
                let _ = tx.try_send(event);
            }
            None
        },
    )
    .map_err(|_| "系统拒绝创建只读事件监听，请检查辅助功能/输入监控权限".to_string())?;

    let source = tap
        .mach_port
        .create_runloop_source(0)
        .map_err(|_| "无法创建输入事件 RunLoop source".to_string())?;
    current.add_source(&source, unsafe { kCFRunLoopCommonModes });
    tap.enable();
    CFRunLoop::run_current();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signal_batch_uses_quiet_period_but_never_waits_over_five_seconds() {
        let start = Instant::now();
        let mut batch = PendingSignals::new(InputSignal::MouseClick, start);
        assert_eq!(batch.deadline(), start + Duration::from_secs(3));

        batch.push(InputSignal::Scroll, start + Duration::from_secs(2));
        assert_eq!(batch.deadline(), start + Duration::from_secs(5));
        batch.push(
            InputSignal::KeyboardActivity,
            start + Duration::from_secs(4),
        );
        assert_eq!(batch.deadline(), start + Duration::from_secs(5));
        assert_eq!(batch.strongest, InputSignal::KeyboardActivity);
    }

    #[test]
    fn keyboard_signal_contains_no_key_or_text_payload() {
        assert!(matches!(
            InputSignal::KeyboardActivity.into_capture_event(),
            CaptureEvent::KeyPause
        ));
    }
}
