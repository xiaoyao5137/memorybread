//! IPC 消息类型定义
//!
//! 本文件中所有类型与 protocol.md § 3-4 的 JSON Schema 严格对应，
//! 通过 serde 自动完成序列化/反序列化。

use serde::{Deserialize, Serialize};

// ─────────────────────────────────────────────────────────────────────────────
// 顶层信封（Envelope）
// ─────────────────────────────────────────────────────────────────────────────

/// 发送给 AI Sidecar 的请求信封
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IpcRequest {
    /// UUID v4，用于关联响应
    pub id: String,
    /// 发送时间戳（Unix 毫秒）
    pub ts: u64,
    /// 具体任务载荷
    pub task: TaskRequest,
}

impl IpcRequest {
    /// 创建一条新请求，自动生成 UUID 和当前时间戳
    pub fn new(task: TaskRequest) -> Self {
        Self {
            id: uuid::Uuid::new_v4().to_string(),
            ts: current_ts_ms(),
            task,
        }
    }
}

/// 从 AI Sidecar 收到的响应信封
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct IpcResponse {
    /// 对应请求的 id
    pub id: String,
    /// 执行状态
    pub status: ResponseStatus,
    /// 成功时的结果，失败时为 None
    pub result: Option<ResultPayload>,
    /// 失败时的错误描述，成功时为 None
    pub error: Option<String>,
    /// Sidecar 内部处理耗时（毫秒）
    pub latency_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ResponseStatus {
    Ok,
    Error,
}

// ─────────────────────────────────────────────────────────────────────────────
// 任务请求载荷（TaskRequest）
// 使用 serde tag 区分类型，序列化为 {"type": "ocr", ...fields}
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum TaskRequest {
    /// 心跳检测
    Ping,
    InteractiveOcrActivity { activity_id: String, active: bool },
    /// 截图 OCR 识别
    Ocr(OcrRequest),
    /// 音频 ASR 转录
    Asr(AsrRequest),
    /// 视觉语言模型理解
    Vlm(VlmRequest),
    /// 文本向量化（批量）
    Embed(EmbedRequest),
    /// PII 敏感信息脱敏
    PiiScrub(PiiScrubRequest),
    /// 用户画像分析
    ProfileAnalysis(ProfileAnalysisRequest),
}

/// OCR 请求
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OcrRequest {
    /// 关联的 captures.id（用于 Sidecar 回写结果时定位记录）
    pub capture_id: i64,
    /// JPEG 截图文件的绝对路径
    pub screenshot_path: String,
    #[serde(default)]
    pub priority: OcrPriority,
}

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OcrPriority {
    #[default]
    Background,
    Foreground,
}

/// ASR 请求
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AsrRequest {
    pub capture_id: i64,
    /// WAV/MP3 音频文件的绝对路径
    pub audio_path: String,
    /// 提示语言，如 "zh"、"en"（传 null 由模型自动检测）
    pub language: Option<String>,
}

/// VLM 视觉理解请求
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VlmRequest {
    pub capture_id: i64,
    pub screenshot_path: String,
    /// 发给 VLM 的自然语言指令
    pub prompt: String,
}

/// 文本向量化请求（支持批量）
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbedRequest {
    pub capture_id: i64,
    /// 待向量化的文本分块列表
    pub texts: Vec<String>,
    /// 使用的 Embedding 模型名，如 "bge-m3"
    pub model: String,
}

/// PII 脱敏请求
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PiiScrubRequest {
    pub capture_id: i64,
    /// 需要脱敏的原始文本
    pub text: String,
}

/// 用户画像分析请求
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProfileAnalysisRequest {
    /// 分析起始日期 (ISO8601)
    pub start_date: String,
    /// 分析结束日期 (ISO8601)
    pub end_date: String,
    /// 现有画像 JSON（用于增量合并）
    pub existing_profile: Option<String>,
}

// ─────────────────────────────────────────────────────────────────────────────
// 任务结果载荷（ResultPayload）
// ─────────────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum ResultPayload {
    Ping(PingResult),
    Ocr(OcrResult),
    Asr(AsrResult),
    Vlm(VlmResult),
    Embed(EmbedResult),
    PiiScrub(PiiScrubResult),
    ProfileAnalysis(ProfileAnalysisResult),
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PingResult {
    pub pong: bool,
    pub sidecar_version: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OcrResult {
    /// OCR 识别的完整文字，段落间用 \n 分隔
    pub text: String,
    /// 整体置信度 0.0~1.0
    pub confidence: f32,
    /// 检测到的主要语言，如 "zh"、"en"
    pub language: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AsrResult {
    /// 转录后的完整文字
    pub text: String,
    pub language: String,
    pub segments: Vec<AsrSegment>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AsrSegment {
    pub start_sec: f32,
    pub end_sec: f32,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VlmResult {
    /// VLM 对屏幕内容的一句话描述
    pub description: String,
    /// 场景分类（见 protocol.md § 4.4）
    pub scene_type: SceneType,
    /// 标签列表
    pub tags: Vec<String>,
}

/// 场景分类枚举
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SceneType {
    DocWriting,
    ImChat,
    Browsing,
    Coding,
    Spreadsheet,
    VideoMeeting,
    Idle,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EmbedResult {
    /// vectors[i] 对应请求中 texts[i] 的向量
    pub vectors: Vec<Vec<f32>>,
    /// 向量维度，如 1024
    pub dimension: usize,
    pub model: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PiiScrubResult {
    /// 脱敏后的文本（敏感内容替换为 [类型标签]）
    pub text: String,
    /// 脱敏的实体数量
    pub redacted_count: usize,
    /// 脱敏的实体类型列表，如 ["PERSON", "PHONE_NUMBER"]
    pub redacted_types: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProfileAnalysisResult {
    /// 合并后的用户画像 JSON
    pub profile: String,
}

// ─────────────────────────────────────────────────────────────────────────────
// 工具函数
// ─────────────────────────────────────────────────────────────────────────────

fn current_ts_ms() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("time went backwards")
        .as_millis() as u64
}

// ─────────────────────────────────────────────────────────────────────────────
// 单元测试：验证序列化/反序列化的往返一致性
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_ocr_request_roundtrip() {
        let req = IpcRequest::new(TaskRequest::Ocr(OcrRequest {
            capture_id: 42,
            screenshot_path: "/tmp/test.jpg".to_string(),
            priority: Default::default(),
        }));
        let json = serde_json::to_string(&req).unwrap();
        let decoded: IpcRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(req.id, decoded.id);
        assert!(json.contains("\"type\":\"ocr\""));
    }

    #[test]
    fn test_embed_request_batch() {
        let req = IpcRequest::new(TaskRequest::Embed(EmbedRequest {
            capture_id: 1,
            texts: vec!["文本一".into(), "文本二".into()],
            model: "bge-m3".into(),
        }));
        let json = serde_json::to_string(&req).unwrap();
        assert!(json.contains("文本一"));
        assert!(json.contains("bge-m3"));
    }

    #[test]
    fn test_response_ok_roundtrip() {
        let resp = IpcResponse {
            id: "test-id".to_string(),
            status: ResponseStatus::Ok,
            result: Some(ResultPayload::Ping(PingResult {
                pong: true,
                sidecar_version: "0.1.0".to_string(),
            })),
            error: None,
            latency_ms: 5,
        };
        let json = serde_json::to_string(&resp).unwrap();
        let decoded: IpcResponse = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.status, ResponseStatus::Ok);
        assert!(decoded.error.is_none());
    }

    #[test]
    fn test_response_error() {
        let resp = IpcResponse {
            id: "test-id".to_string(),
            status: ResponseStatus::Error,
            result: None,
            error: Some("FILE_NOT_FOUND: /tmp/missing.jpg".to_string()),
            latency_ms: 1,
        };
        let json = serde_json::to_string(&resp).unwrap();
        assert!(json.contains("FILE_NOT_FOUND"));
    }

    #[test]
    fn test_scene_type_serialization() {
        let result = VlmResult {
            description: "用户在写文档".into(),
            scene_type: SceneType::DocWriting,
            tags: vec!["文档".into()],
        };
        let json = serde_json::to_string(&result).unwrap();
        assert!(json.contains("\"doc_writing\""));
    }
}
