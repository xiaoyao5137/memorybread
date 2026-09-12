//! IPC 客户端 — 与 AI Sidecar 通信
//!
//! 通过 Unix Domain Socket 调用 AI Sidecar 的 OCR/Embed/ASR/VLM 功能。

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::time::Duration;
use thiserror::Error;
use tracing::debug;

const DEFAULT_IPC_TIMEOUT: Duration = Duration::from_secs(10);
const DEFAULT_OCR_IPC_TIMEOUT: Duration = Duration::from_secs(90);
const DEFAULT_IPC_SOCKET_PATH: &str = "/tmp/memory-bread-sidecar.sock";
const IPC_SOCKET_ENV: &str = "MEMORY_BREAD_IPC_SOCKET";

/// IPC 客户端错误
#[derive(Debug, Error)]
pub enum IpcError {
    #[error("连接失败: {0}")]
    ConnectionFailed(String),

    #[error("序列化错误: {0}")]
    SerializationError(#[from] serde_json::Error),

    #[error("IO 错误: {0}")]
    IoError(#[from] std::io::Error),

    #[error("Sidecar 返回错误: {0}")]
    SidecarError(String),

    #[error("超时")]
    Timeout,
}

/// IPC 请求
#[derive(Debug, Serialize)]
struct IpcRequest {
    id: String,
    ts: i64,
    task: Task,
}

/// 任务类型
#[derive(Debug, Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Task {
    Ocr {
        capture_id: i64,
        screenshot_path: String,
        priority: &'static str,
    },
    Embed {
        capture_id: i64,
        texts: Vec<String>,
    },
}

/// IPC 响应
#[derive(Debug, Deserialize)]
struct IpcResponse {
    id: String,
    status: String,
    result: Option<serde_json::Value>,
    error: Option<String>,
}

/// OCR 结果
#[derive(Debug, Deserialize)]
pub struct OcrResult {
    pub text: String,
    pub confidence: f64,
}

/// Embedding 结果
#[derive(Debug, Deserialize)]
pub struct EmbedResult {
    pub vectors: Vec<Vec<f32>>,
}

/// IPC 客户端
#[derive(Clone)]
pub struct IpcClient {
    socket_path: String,
    timeout: Duration,
    ocr_timeout: Duration,
}

impl IpcClient {
    /// 创建默认客户端；安装版可通过环境变量使用隔离的 Unix Socket。
    pub fn new() -> Self {
        Self {
            socket_path: std::env::var(IPC_SOCKET_ENV)
                .ok()
                .filter(|value| !value.trim().is_empty())
                .unwrap_or_else(|| DEFAULT_IPC_SOCKET_PATH.to_string()),
            // OCR 是同步系统调用，客户端超时不能取消服务端正在运行的 Vision 任务。
            // 使用覆盖最慢正常请求的单次截止时间，禁止超时后在后台继续重试。
            timeout: DEFAULT_IPC_TIMEOUT,
            ocr_timeout: DEFAULT_OCR_IPC_TIMEOUT,
        }
    }

    #[cfg(test)]
    pub(crate) fn with_socket_path_and_timeout(
        socket_path: impl Into<String>,
        timeout: Duration,
    ) -> Self {
        Self {
            socket_path: socket_path.into(),
            timeout,
            ocr_timeout: timeout,
        }
    }

    /// 快速检查 Sidecar 是否在线（带超时）
    pub async fn ping(&self) -> bool {
        let socket_path = self.socket_path.clone();
        let result = tokio::time::timeout(
            Duration::from_secs(1),
            tokio::task::spawn_blocking(move || UnixStream::connect(&socket_path).is_ok()),
        )
        .await;

        matches!(result, Ok(Ok(true)))
    }

    /// 调用 OCR
    pub fn call_ocr(&self, capture_id: i64, screenshot_path: &str) -> Result<OcrResult, IpcError> {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64;

        let request = IpcRequest {
            id: uuid::Uuid::new_v4().to_string(),
            ts,
            task: Task::Ocr {
                capture_id,
                screenshot_path: screenshot_path.to_string(),
                priority: "background",
            },
        };

        let response = self.send_request_with_timeout(&request, self.ocr_timeout)?;

        if response.status != "ok" {
            let error_msg = response.error.unwrap_or_else(|| "未知错误".to_string());
            return Err(IpcError::SidecarError(error_msg));
        }

        let result = response
            .result
            .ok_or_else(|| IpcError::SidecarError("缺少 result 字段".to_string()))?;

        let ocr_result: OcrResult = serde_json::from_value(result)?;
        Ok(ocr_result)
    }

    /// 调用 Embedding（文本向量化）
    pub fn call_embed(&self, capture_id: i64, texts: Vec<String>) -> Result<EmbedResult, IpcError> {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64;

        let request = IpcRequest {
            id: uuid::Uuid::new_v4().to_string(),
            ts,
            task: Task::Embed { capture_id, texts },
        };

        let response = self.send_request(&request)?;

        if response.status != "ok" {
            let error_msg = response.error.unwrap_or_else(|| "未知错误".to_string());
            return Err(IpcError::SidecarError(error_msg));
        }

        let result = response
            .result
            .ok_or_else(|| IpcError::SidecarError("缺少 result 字段".to_string()))?;

        let embed_result: EmbedResult = serde_json::from_value(result)?;
        Ok(embed_result)
    }

    /// 发送请求并接收响应
    fn send_request(&self, request: &IpcRequest) -> Result<IpcResponse, IpcError> {
        self.send_request_with_timeout(request, self.timeout)
    }

    fn send_request_with_timeout(
        &self,
        request: &IpcRequest,
        timeout: Duration,
    ) -> Result<IpcResponse, IpcError> {
        // 连接到 Unix Socket
        let mut stream = UnixStream::connect(&self.socket_path).map_err(|e| {
            IpcError::ConnectionFailed(format!(
                "无法连接到 AI Sidecar ({}): {}",
                self.socket_path, e
            ))
        })?;

        stream.set_read_timeout(Some(timeout))?;
        stream.set_write_timeout(Some(timeout))?;

        // 序列化请求
        let request_json = serde_json::to_string(request)?;
        let request_bytes = request_json.as_bytes();

        // 发送请求（长度前缀 + JSON）
        let length = (request_bytes.len() as u32).to_be_bytes();
        stream.write_all(&length).map_err(Self::map_io_error)?;
        stream
            .write_all(request_bytes)
            .map_err(Self::map_io_error)?;
        stream.flush().map_err(Self::map_io_error)?;

        debug!("发送 IPC 请求: {} 字节", request_bytes.len());

        // 读取响应长度
        let mut length_buf = [0u8; 4];
        stream
            .read_exact(&mut length_buf)
            .map_err(Self::map_io_error)?;
        let response_length = u32::from_be_bytes(length_buf) as usize;

        if response_length > 10 * 1024 * 1024 {
            // 10MB 限制
            return Err(IpcError::SidecarError(format!(
                "响应过大: {} 字节",
                response_length
            )));
        }

        // 读取响应内容
        let mut response_buf = vec![0u8; response_length];
        stream
            .read_exact(&mut response_buf)
            .map_err(Self::map_io_error)?;

        debug!("接收 IPC 响应: {} 字节", response_length);

        // 反序列化响应
        let response: IpcResponse = serde_json::from_slice(&response_buf)?;
        Ok(response)
    }

    fn map_io_error(error: std::io::Error) -> IpcError {
        if matches!(
            error.kind(),
            std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
        ) {
            IpcError::Timeout
        } else {
            IpcError::IoError(error)
        }
    }

    /// 检查 Sidecar 是否可用
    pub fn is_available(&self) -> bool {
        std::path::Path::new(&self.socket_path).exists()
    }
}

impl Default for IpcClient {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ipc_client_creation() {
        let client = IpcClient::new();
        assert_eq!(client.socket_path, "/tmp/memory-bread-sidecar.sock");
        assert_eq!(client.timeout, DEFAULT_IPC_TIMEOUT);
        assert_eq!(client.ocr_timeout, DEFAULT_OCR_IPC_TIMEOUT);
    }
}
