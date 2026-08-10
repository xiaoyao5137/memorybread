//! 核心采集引擎模块
//!
//! 职责：
//! - 接收来自事件监听器的触发信号
//! - 执行截图 + Accessibility Tree 文本抓取
//! - 隐私窗口过滤（密码框 / 黑名单应用）
//! - 写入 SQLite + 发送给 AI Sidecar（OCR/Embed）

pub mod ax;
pub mod blacklist;
pub mod content_filter;
pub mod engine;
pub mod filter;
pub mod listener;
pub mod screenshot;
pub mod signal;

pub use blacklist::BlacklistChecker;
pub use engine::{CaptureConfig, CaptureEngine, CaptureEvent};
pub use filter::PrivacyFilter;
pub use listener::{start_context_watcher, start_listener, CaptureSchedule, ListenerConfig};
pub use screenshot::ScreenshotResult;
pub use signal::{start_input_signal_listener, start_signal_aggregator};

use thiserror::Error;

/// 采集模块统一错误类型
#[derive(Debug, Error)]
pub enum CaptureError {
    #[error("截图失败: {0}")]
    ScreenshotFailed(String),

    #[error("图片处理错误: {0}")]
    ImageError(String),

    #[error("存储错误: {0}")]
    Storage(#[from] crate::storage::StorageError),

    #[error("IO 错误: {0}")]
    Io(#[from] std::io::Error),
}
