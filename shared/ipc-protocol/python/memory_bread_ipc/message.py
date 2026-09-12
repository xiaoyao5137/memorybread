"""
IPC 消息类型定义（Python 端）

使用 Pydantic v2 定义，与 Rust 端 message.rs 中的 serde 类型严格对应。
序列化策略：serde tag "type" + snake_case 字段名，与 Rust 端一致。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_serializer, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# 任务请求载荷
# 每个 Request 类都带有 type: Literal[...] 字段，用于 serde tag 区分
# ─────────────────────────────────────────────────────────────────────────────

class PingRequest(BaseModel):
    type: Literal["ping"] = "ping"


class OcrRequest(BaseModel):
    type:            Literal["ocr"] = "ocr"
    capture_id:      int
    screenshot_path: str
    priority: Literal["background", "foreground"] = "background"


class InteractiveOcrActivityRequest(BaseModel):
    type: Literal["interactive_ocr_activity"] = "interactive_ocr_activity"
    activity_id: str = Field(min_length=1, max_length=128)
    active: bool


class AsrRequest(BaseModel):
    type:       Literal["asr"] = "asr"
    capture_id: int
    audio_path: str
    language:   Optional[str] = None  # None 时模型自动检测


class VlmRequest(BaseModel):
    type:            Literal["vlm"] = "vlm"
    capture_id:      int
    screenshot_path: str
    prompt:          str


class EmbedRequest(BaseModel):
    type:       Literal["embed"] = "embed"
    capture_id: int
    texts:      list[str]
    model:      str = "bge-m3"


class PiiScrubRequest(BaseModel):
    type:       Literal["pii_scrub"] = "pii_scrub"
    capture_id: int
    text:       str


class ProfileAnalysisRequest(BaseModel):
    type:             Literal["profile_analysis"] = "profile_analysis"
    start_date:       str  # ISO8601 date
    end_date:         str  # ISO8601 date
    existing_profile: Optional[str] = None


class RagRequest(BaseModel):
    type:  Literal["rag"] = "rag"
    query: str
    top_k: int = 5


# Union 用于解析时自动根据 type 字段分派
TaskRequest = Annotated[
    Union[
        PingRequest,
        OcrRequest,
        InteractiveOcrActivityRequest,
        AsrRequest,
        VlmRequest,
        EmbedRequest,
        PiiScrubRequest,
        ProfileAnalysisRequest,
        RagRequest,
    ],
    Field(discriminator="type"),
]


# ─────────────────────────────────────────────────────────────────────────────
# 请求信封
# ─────────────────────────────────────────────────────────────────────────────

class IpcRequest(BaseModel):
    """发送给 Sidecar 的请求信封（由 Rust 端发来，Python 端解析）"""
    id:   str         # UUID v4
    ts:   int         # Unix 毫秒
    task: TaskRequest = Field(discriminator="type")  # type: ignore[assignment]

    @classmethod
    def _task_adapter(cls):
        from pydantic import TypeAdapter
        return TypeAdapter(TaskRequest)


# ─────────────────────────────────────────────────────────────────────────────
# 任务结果载荷
# ─────────────────────────────────────────────────────────────────────────────

class PingResult(BaseModel):
    type:            Literal["ping"] = "ping"
    pong:            bool = True
    sidecar_version: str  = "0.1.0"


class OcrResult(BaseModel):
    type:       Literal["ocr"] = "ocr"
    text:       str
    confidence: float = 0.0
    language:   str   = "zh"


class AsrSegment(BaseModel):
    start_sec: float
    end_sec:   float
    text:      str


class AsrResult(BaseModel):
    type:     Literal["asr"] = "asr"
    text:     str
    language: str            = "zh"
    segments: list[AsrSegment] = Field(default_factory=list)


class SceneType(str, Enum):
    DOC_WRITING  = "doc_writing"
    IM_CHAT      = "im_chat"
    BROWSING     = "browsing"
    CODING       = "coding"
    SPREADSHEET  = "spreadsheet"
    VIDEO_MEETING = "video_meeting"
    IDLE         = "idle"
    UNKNOWN      = "unknown"


class VlmResult(BaseModel):
    type:        Literal["vlm"] = "vlm"
    description: str
    scene_type:  SceneType      = SceneType.UNKNOWN
    tags:        list[str]      = Field(default_factory=list)


class EmbedResult(BaseModel):
    type:      Literal["embed"] = "embed"
    vectors:   list[list[float]]
    dimension: int
    model:     str


class PiiScrubResult(BaseModel):
    type:           Literal["pii_scrub"] = "pii_scrub"
    text:           str
    redacted_count: int       = 0
    redacted_types: list[str] = Field(default_factory=list)


class ProfileAnalysisResult(BaseModel):
    type:    Literal["profile_analysis"] = "profile_analysis"
    profile: str  # JSON string


class RagResult(BaseModel):
    type:     Literal["rag"] = "rag"
    answer:   str
    contexts: list[dict[str, Any]] = Field(default_factory=list)
    model:    str = ""


ResultPayload = Annotated[
    Union[
        PingResult,
        OcrResult,
        AsrResult,
        VlmResult,
        EmbedResult,
        PiiScrubResult,
        ProfileAnalysisResult,
        RagResult,
    ],
    Field(discriminator="type"),
]


# ─────────────────────────────────────────────────────────────────────────────
# 响应信封
# ─────────────────────────────────────────────────────────────────────────────

class ResponseStatus(str, Enum):
    OK    = "ok"
    ERROR = "error"


class IpcResponse(BaseModel):
    """Sidecar 发回给 Rust Engine 的响应信封"""
    id:         str
    status:     ResponseStatus
    # result 实际类型为 ResultPayload | None；使用 Any 规避 Pydantic v2
    # 对复杂 Annotated Union 字段的序列化限制，通过 field_serializer 手动处理
    result:     Any = None
    error:      Optional[str]         = None
    latency_ms: int                  = 0

    @field_serializer("result")
    def _serialize_result(self, v: Any) -> Any:
        if v is None:
            return None
        if hasattr(v, "model_dump"):
            return v.model_dump(mode="json")
        return v

    @classmethod
    def make_ok(cls, req_id: str, result: Any, latency_ms: int = 0) -> "IpcResponse":
        return cls(
            id=req_id,
            status=ResponseStatus.OK,
            result=result,
            latency_ms=latency_ms,
        )

    @classmethod
    def make_error(cls, req_id: str, code: str, message: str, latency_ms: int = 0) -> "IpcResponse":
        return cls(
            id=req_id,
            status=ResponseStatus.ERROR,
            error=f"{code}: {message}",
            latency_ms=latency_ms,
        )

    def to_frame(self) -> bytes:
        """序列化为带 4 字节大端 length header 的完整帧"""
        import json
        # 手动构建 dict，规避 Pydantic v2 对 classmethod/复杂 Union 字段的序列化限制
        data: dict = {
            "id":         self.id,
            "status":     self.status.value,
            "latency_ms": self.latency_ms,
            "error":      self.error,
            "result":     None,
        }
        if self.result is not None:
            r = self.result
            data["result"] = r.model_dump(mode="json") if hasattr(r, "model_dump") else r

        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return len(payload).to_bytes(4, "big") + payload
