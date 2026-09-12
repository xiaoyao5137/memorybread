//! IPC 传输层：帧编解码 + 连接管理
//!
//! 帧格式：[4字节大端 uint32 消息长度][N字节 UTF-8 JSON]
//!
//! 平台路由：
//! - macOS/Linux：Unix Domain Socket（/tmp/memory-bread-sidecar.sock）
//! - Windows：TCP Loopback（127.0.0.1:17071）

use std::time::Duration;

#[cfg(windows)]
use tokio::net::TcpStream;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    time::timeout,
};
use tracing::{debug, warn};

use crate::{
    error::IpcError,
    message::{IpcRequest, IpcResponse, ResponseStatus, TaskRequest},
};

// ─────────────────────────────────────────────────────────────────────────────
// 常量
// ─────────────────────────────────────────────────────────────────────────────

/// 单条消息最大字节数（16 MB）
const MAX_MESSAGE_BYTES: usize = 16 * 1024 * 1024;

/// macOS/Linux Unix Socket 默认路径；安装版可通过环境变量隔离实例。
#[cfg(unix)]
pub const UNIX_SOCKET_PATH: &str = "/tmp/memory-bread-sidecar.sock";
#[cfg(unix)]
const IPC_SOCKET_ENV: &str = "MEMORY_BREAD_IPC_SOCKET";

#[cfg(unix)]
fn unix_socket_path() -> String {
    std::env::var(IPC_SOCKET_ENV)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| UNIX_SOCKET_PATH.to_string())
}

/// Windows TCP 端口
pub const TCP_PORT: u16 = 17071;

/// 等待 Sidecar 就绪的最大秒数
const SIDECAR_READY_TIMEOUT_SECS: u64 = 30;

/// 每次 ping 重试间隔
const PING_RETRY_INTERVAL_MS: u64 = 500;

// ─────────────────────────────────────────────────────────────────────────────
// 帧编解码（平台无关）
// ─────────────────────────────────────────────────────────────────────────────

/// 将 IpcRequest 编码为 [length(4B BE)] + [JSON bytes]
pub fn encode_request(req: &IpcRequest) -> Result<Vec<u8>, IpcError> {
    let payload = serde_json::to_vec(req)?;
    if payload.len() > MAX_MESSAGE_BYTES {
        return Err(IpcError::MessageTooLarge {
            size: payload.len(),
            max: MAX_MESSAGE_BYTES,
        });
    }
    let mut buf = Vec::with_capacity(4 + payload.len());
    buf.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    buf.extend_from_slice(&payload);
    Ok(buf)
}

/// 从已读取的字节中解码 IpcResponse（不含4字节 length header）
pub fn decode_response(bytes: &[u8]) -> Result<IpcResponse, IpcError> {
    Ok(serde_json::from_slice(bytes)?)
}

// ─────────────────────────────────────────────────────────────────────────────
// 平台适配：Stream 抽象
// ─────────────────────────────────────────────────────────────────────────────

/// 跨平台的 IPC 连接，内部持有底层 Stream。
/// 在 macOS/Linux 使用 Unix Domain Socket，Windows 使用 TCP。
pub struct IpcClient {
    #[cfg(unix)]
    stream: tokio::net::UnixStream,
    #[cfg(windows)]
    stream: TcpStream,
}

impl IpcClient {
    // ── 连接建立 ─────────────────────────────────────────────────────────────

    /// 使用平台默认地址建立连接
    pub async fn connect_default() -> Result<Self, IpcError> {
        #[cfg(unix)]
        {
            let socket_path = unix_socket_path();
            Self::connect_unix(&socket_path).await
        }
        #[cfg(windows)]
        {
            Self::connect_tcp("127.0.0.1", TCP_PORT).await
        }
    }

    /// 连接 Unix Domain Socket（macOS/Linux）
    #[cfg(unix)]
    pub async fn connect_unix(path: &str) -> Result<Self, IpcError> {
        let stream = tokio::net::UnixStream::connect(path).await?;
        debug!("IPC 已连接 Unix Socket: {}", path);
        Ok(Self { stream })
    }

    /// 连接 TCP Loopback（Windows）
    #[cfg(windows)]
    pub async fn connect_tcp(host: &str, port: u16) -> Result<Self, IpcError> {
        let addr = format!("{}:{}", host, port);
        let stream = TcpStream::connect(&addr).await?;
        // 对于本地 loopback，关闭 Nagle 算法以降低延迟
        stream.set_nodelay(true)?;
        debug!("IPC 已连接 TCP: {}", addr);
        Ok(Self { stream })
    }

    /// 等待 Sidecar 启动就绪（带超时重试的 ping 握手）
    ///
    /// 场景：Core Engine 先于 Sidecar 启动，需要轮询直到 Sidecar 就绪。
    pub async fn wait_for_sidecar() -> Result<Self, IpcError> {
        let deadline = std::time::Instant::now() + Duration::from_secs(SIDECAR_READY_TIMEOUT_SECS);

        loop {
            let remaining = deadline
                .checked_duration_since(std::time::Instant::now())
                .ok_or(IpcError::SidecarTimeout {
                    seconds: SIDECAR_READY_TIMEOUT_SECS,
                })?;

            match timeout(Duration::from_secs(1), Self::connect_default()).await {
                Ok(Ok(mut client)) => {
                    // 连接成功，发送 ping 确认 Sidecar 已完成模型加载
                    match client.ping().await {
                        Ok(_) => {
                            debug!("Sidecar 就绪");
                            return Ok(client);
                        }
                        Err(e) => {
                            warn!("Sidecar 连接成功但 ping 失败: {}，继续等待", e);
                        }
                    }
                }
                Ok(Err(_)) | Err(_) => {
                    debug!("Sidecar 尚未就绪，剩余等待 {:.1}s", remaining.as_secs_f32());
                }
            }

            tokio::time::sleep(Duration::from_millis(PING_RETRY_INTERVAL_MS)).await;
        }
    }

    // ── 发送/接收 ─────────────────────────────────────────────────────────────

    /// 发送一个任务请求并等待响应（同步阻塞当前 async task）
    pub async fn send(&mut self, task: TaskRequest) -> Result<IpcResponse, IpcError> {
        let req = IpcRequest::new(task);
        let req_id = req.id.clone();

        // 编码并写入
        let frame = encode_request(&req)?;
        self.write_all(&frame).await?;
        debug!("已发送请求 id={} type={:?}", req_id, req.task.type_name());

        // 读取响应帧
        let resp = self.read_response().await?;

        // 验证响应 ID 对应
        if resp.id != req_id {
            return Err(IpcError::IdMismatch {
                expected: req_id,
                actual: resp.id,
            });
        }

        // 如果 Sidecar 返回错误状态，转换为 Rust 错误
        if resp.status == ResponseStatus::Error {
            let msg = resp.error.unwrap_or_else(|| "unknown sidecar error".into());
            return Err(IpcError::SidecarError(msg));
        }

        Ok(resp)
    }

    /// 发送 ping 心跳
    pub async fn ping(&mut self) -> Result<IpcResponse, IpcError> {
        self.send(TaskRequest::Ping).await
    }

    // ── 内部 IO ──────────────────────────────────────────────────────────────

    async fn write_all(&mut self, buf: &[u8]) -> Result<(), IpcError> {
        #[cfg(unix)]
        self.stream.write_all(buf).await?;
        #[cfg(windows)]
        self.stream.write_all(buf).await?;
        Ok(())
    }

    async fn read_response(&mut self) -> Result<IpcResponse, IpcError> {
        // 读取 4 字节 length header
        let mut len_buf = [0u8; 4];
        #[cfg(unix)]
        self.stream.read_exact(&mut len_buf).await?;
        #[cfg(windows)]
        self.stream.read_exact(&mut len_buf).await?;

        let msg_len = u32::from_be_bytes(len_buf) as usize;
        if msg_len > MAX_MESSAGE_BYTES {
            return Err(IpcError::MessageTooLarge {
                size: msg_len,
                max: MAX_MESSAGE_BYTES,
            });
        }

        // 读取 JSON payload
        let mut payload = vec![0u8; msg_len];
        #[cfg(unix)]
        self.stream.read_exact(&mut payload).await?;
        #[cfg(windows)]
        self.stream.read_exact(&mut payload).await?;

        decode_response(&payload)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// TaskRequest 辅助方法
// ─────────────────────────────────────────────────────────────────────────────

impl TaskRequest {
    /// 返回任务类型名称字符串（用于日志）
    pub fn type_name(&self) -> &'static str {
        match self {
            TaskRequest::Ping => "ping",
            TaskRequest::Ocr(_) => "ocr",
            TaskRequest::InteractiveOcrActivity { .. } => "interactive_ocr_activity",
            TaskRequest::Asr(_) => "asr",
            TaskRequest::Vlm(_) => "vlm",
            TaskRequest::Embed(_) => "embed",
            TaskRequest::PiiScrub(_) => "pii_scrub",
            TaskRequest::ProfileAnalysis(_) => "profile_analysis",
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 帧编解码单元测试
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::message::{IpcRequest, OcrRequest, TaskRequest};

    #[test]
    fn test_encode_decode_frame() {
        let req = IpcRequest::new(TaskRequest::Ocr(OcrRequest {
            capture_id: 1,
            screenshot_path: "/tmp/shot.jpg".into(),
            priority: Default::default(),
        }));

        let frame = encode_request(&req).unwrap();

        // 前4字节为大端 uint32 长度
        let len = u32::from_be_bytes(frame[..4].try_into().unwrap()) as usize;
        assert_eq!(len, frame.len() - 4);

        // 解码 JSON payload
        let payload = &frame[4..];
        let decoded: IpcRequest = serde_json::from_slice(payload).unwrap();
        assert_eq!(decoded.id, req.id);
    }

    #[test]
    fn test_encode_too_large_fails() {
        let huge_text = "x".repeat(MAX_MESSAGE_BYTES + 1);
        let req = IpcRequest::new(TaskRequest::Ocr(OcrRequest {
            capture_id: 1,
            screenshot_path: huge_text,
            priority: Default::default(),
        }));
        let result = encode_request(&req);
        assert!(matches!(result, Err(IpcError::MessageTooLarge { .. })));
    }
}
