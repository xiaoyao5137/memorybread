//! StorageError — storage 层统一错误类型

use thiserror::Error;

#[derive(Debug, Error)]
pub enum StorageError {
    #[error("DOCUMENT_AUTOMATIC_WRITES_PAUSED")]
    DocumentAutomaticWritesPaused { bucket: u8 },
    #[error("SOURCE_WRITES_PAUSED")]
    DocumentSourceWritesPaused,
    #[error("SQLite 错误: {0}")]
    Sqlite(#[from] rusqlite::Error),

    #[error("JSON 序列化错误: {0}")]
    Json(#[from] serde_json::Error),

    #[error("文件系统错误: {0}")]
    Io(#[from] std::io::Error),

    #[error("记录不存在: {0}")]
    NotFound(String),

    #[error("数据库迁移失败（版本 {version}）: {reason}")]
    MigrationFailed {
        version: &'static str,
        reason: String,
    },

    #[error("连接锁已中毒（Mutex poisoned）")]
    LockPoisoned,

    #[error("tokio 任务 join 失败: {0}")]
    JoinError(#[from] tokio::task::JoinError),
}

// Mutex PoisonError 无法直接 #[from]，手动实现
impl<T> From<std::sync::PoisonError<T>> for StorageError {
    fn from(_: std::sync::PoisonError<T>) -> Self {
        StorageError::LockPoisoned
    }
}
