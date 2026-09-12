//! 记忆面包 IPC 协议库
//!
//! 供 core-engine 中的所有 Rust crate 引用，定义与 AI Sidecar（Python）
//! 通信的完整协议类型和传输层工具。
//!
//! # 快速示例
//!
//! ```rust,no_run
//! use memory_bread_ipc::{IpcClient, TaskRequest, OcrRequest};
//!
//! #[tokio::main]
//! async fn main() -> Result<(), Box<dyn std::error::Error>> {
//!     let mut client = IpcClient::connect_default().await?;
//!
//!     let resp = client.send(TaskRequest::Ocr(OcrRequest {
//!         capture_id:      1,
//!         screenshot_path: "/tmp/shot.jpg".into(),
//!         priority: Default::default(),
//!     })).await?;
//!
//!     println!("OCR result: {:?}", resp.result);
//!     Ok(())
//! }
//! ```

pub mod error;
pub mod message;
pub mod transport;

pub use error::IpcError;
pub use message::{
    AsrRequest, AsrResult, EmbedRequest, EmbedResult, IpcRequest, IpcResponse, OcrRequest,
    OcrResult, PiiScrubRequest, PiiScrubResult, ProfileAnalysisRequest, ProfileAnalysisResult,
    ResultPayload, TaskRequest, VlmRequest, VlmResult,
};
pub use transport::IpcClient;
