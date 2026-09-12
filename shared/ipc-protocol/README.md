# ipc-protocol — 进程间通信协议组件

Core Engine（Rust）↔ AI Sidecar（Python）的完整 IPC 协议定义与实现。

## 文件结构

```
shared/ipc-protocol/
├── protocol.md          # 协议规范文档（传输层、帧格式、消息结构、错误码）
├── schema/
│   ├── request.json     # 请求 JSON Schema（共 6 种 task type）
│   └── response.json    # 响应 JSON Schema（共 6 种 result type）
├── rust/                # Rust 端实现（作为 library crate 被 core-engine 引用）
│   ├── Cargo.toml
│   └── src/
│       ├── lib.rs       # 公开 API 入口
│       ├── message.rs   # 所有消息类型（IpcRequest/Response + TaskRequest/ResultPayload）
│       ├── transport.rs # IpcClient：帧编解码 + 跨平台连接管理
│       └── error.rs     # IpcError 枚举
└── python/              # Python 端实现（被 ai-sidecar 引用）
    ├── __init__.py      # 包入口，统一导出
    ├── message.py       # Pydantic v2 消息类型（与 Rust 端严格对应）
    ├── transport.py     # IpcServer：asyncio Socket 服务端 + FrameCodec
    └── requirements.txt
```

## 传输层一览

| 平台 | 方式 | 地址 |
|------|------|------|
| macOS / Linux | Unix Domain Socket | 默认 `/tmp/memory-bread-sidecar.sock`；安装版由 `MEMORY_BREAD_IPC_SOCKET` 指定隔离路径 |
| Windows | TCP Loopback | `127.0.0.1:17071` |

Rust 客户端与 Python 服务端必须读取同一个 `MEMORY_BREAD_IPC_SOCKET` 值。未设置时保持默认地址，供开发环境使用；安装版必须设置隔离路径，避免与同时运行的开发 Sidecar 冲突。

帧格式：`[4字节大端 uint32 消息长度] + [N字节 UTF-8 JSON]`

## 支持的任务类型

| type | 方向 | 功能 |
|------|------|------|
| `ping` | Engine → Sidecar | 心跳 / 就绪检测 |
| `ocr` | Engine → Sidecar | 截图文字识别 |
| `asr` | Engine → Sidecar | 音频语音转文字 |
| `vlm` | Engine → Sidecar | 视觉语言理解 |
| `embed` | Engine → Sidecar | 文本批量向量化 |
| `pii_scrub` | Engine → Sidecar | 敏感信息脱敏 |

## Rust 端使用方式

```rust
// 在 core-engine/Cargo.toml 中添加：
// memory-bread-ipc = { path = "../../shared/ipc-protocol/rust" }

use memory_bread_ipc::{IpcClient, TaskRequest, OcrRequest};

// 等待 Sidecar 就绪（自动 ping 握手，最多等 30s）
let mut client = IpcClient::wait_for_sidecar().await?;

// 发送 OCR 任务
let resp = client.send(TaskRequest::Ocr(OcrRequest {
    capture_id:      42,
    screenshot_path: "/tmp/shot.jpg".to_string(),
})).await?;

if let Some(ResultPayload::Ocr(result)) = resp.result {
    println!("识别文字: {}", result.text);
}
```

## Python 端使用方式

```python
# ai-sidecar/main.py

import asyncio
from memory_bread_ipc import IpcServer, IpcRequest, IpcResponse
from memory_bread_ipc import OcrResult, PingResult

async def dispatch(req: IpcRequest) -> IpcResponse:
    import time
    t0 = time.monotonic()
    task = req.task

    if task.type == "ping":
        return IpcResponse.ok(req.id, PingResult(), int((time.monotonic()-t0)*1000))

    if task.type == "ocr":
        text = run_ocr(task.screenshot_path)   # 调用真实 OCR
        return IpcResponse.ok(req.id, OcrResult(text=text, confidence=0.95, language="zh"),
                              int((time.monotonic()-t0)*1000))

    return IpcResponse.error(req.id, "NOT_IMPLEMENTED", f"task {task.type}")

server = IpcServer(dispatch_fn=dispatch)
asyncio.run(server.serve())
```

## 错误码列表

| 错误码 | 含义 |
|--------|------|
| `FILE_NOT_FOUND` | 截图/音频文件路径不存在 |
| `MODEL_NOT_LOADED` | AI 模型未加载 |
| `OCR_FAILED` | OCR 识别失败 |
| `ASR_FAILED` | ASR 转录失败 |
| `VLM_FAILED` | VLM 推理失败 |
| `EMBED_FAILED` | 向量化失败 |
| `PII_FAILED` | 脱敏失败 |
| `INVALID_REQUEST` | 请求字段缺失/类型错误 |
| `NOT_IMPLEMENTED` | 任务类型未实现 |
| `INTERNAL_ERROR` | Sidecar 内部未知错误 |


## OCR scheduling

OCR requests accept `priority: background | foreground` (legacy default: background).
User consultation/creation requests explicitly use foreground; capture/backfill and automatic scanning use background.
`interactive_ocr_activity` carries `activity_id` and `active`; live user operations renew every 5 seconds,
release on success/failure/cancellation, and expire after 15 seconds without renewal.
Multiple operations are reference counted by unique ID. Background OCR is deferred while any user operation
or existing P0 inference demand is active. `OCR_DEFERRED` is a scheduling response, not an OCR failure:
the capture consumer retains the same job and retries after 250ms. A cancelled image restarts in full;
no partial OCR result is committed. Backend cancellation is cooperative (Vision supports request cancellation);
unsupported engines finish the current native call before yielding, with global concurrency still one.
