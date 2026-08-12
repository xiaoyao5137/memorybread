# IPC 协议规范 — 记忆面包 Core Engine ↔ AI Sidecar

版本: v1.0 | 日期: 2026-03-04

---

## 1. 概述

Core Engine（Rust）与 AI Sidecar（Python）是两个独立进程，通过本地 Socket 通信。

```
┌─────────────────────────────┐          ┌──────────────────────────────┐
│   Core Engine (Rust)        │          │   AI Sidecar (Python)        │
│                             │          │                              │
│  capture/ ──> IpcClient ───────────────────> IpcServer               │
│                             │  Socket  │      ├── OcrWorker           │
│  api/     <── IpcClient <──────────────────── IpcServer              │
│                             │          │      ├── AsrWorker           │
└─────────────────────────────┘          │      ├── VlmWorker           │
                                         │      ├── EmbedWorker         │
                                         │      └── PiiScrubWorker      │
                                         └──────────────────────────────┘
```

---

## 2. 传输层

### 2.1 连接方式

| 平台 | 传输方式 | 地址 |
|------|---------|------|
| macOS / Linux | Unix Domain Socket | 默认 `/tmp/memory-bread-sidecar.sock`；安装版由 `MEMORY_BREAD_IPC_SOCKET` 指定隔离路径 |
| Windows | TCP Loopback | `127.0.0.1:17071` |

Rust 端和 Python 端必须使用同一个 `MEMORY_BREAD_IPC_SOCKET`。未设置时使用默认路径；安装版设置隔离路径，以支持与开发环境并行运行。

Rust 端在运行时自动检测平台，选择对应的连接方式。Python 端同理。

### 2.2 帧格式（Length-Prefixed JSON）

每条消息由两部分组成，顺序写入 Socket：

```
┌─────────────────────┬──────────────────────────────────────────────┐
│  Header (4 bytes)   │  Payload (N bytes)                           │
│  uint32 大端序       │  UTF-8 编码的 JSON 字符串                     │
│  表示 Payload 字节数 │                                              │
└─────────────────────┴──────────────────────────────────────────────┘
```

- 最大消息体：**16 MB**（防止嵌入向量或大截图 Base64 撑爆内存）
- 连接复用：一个 TCP/Socket 连接支持多次请求-响应（Keep-Alive）
- 并发模型：Python Sidecar 为每个连接创建独立协程，支持并发处理

---

## 3. 消息结构

### 3.1 请求（Request）

```json
{
  "id":   "550e8400-e29b-41d4-a716-446655440000",
  "ts":   1709510400000,
  "task": {
    "type": "ocr",
    ...task-specific fields...
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string (UUID v4) | 请求唯一标识，用于关联响应 |
| `ts` | uint64 | 发送时间戳（Unix 毫秒） |
| `task` | TaskPayload | 任务载荷，`type` 字段区分任务类型 |

### 3.2 响应（Response）

```json
{
  "id":         "550e8400-e29b-41d4-a716-446655440000",
  "status":     "ok",
  "result":     { ...task-specific result... },
  "error":      null,
  "latency_ms": 87
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 对应请求的 `id` |
| `status` | `"ok"` \| `"error"` | 执行状态 |
| `result` | ResultPayload \| null | 成功时的结果，错误时为 null |
| `error` | string \| null | 错误描述，成功时为 null |
| `latency_ms` | uint64 | Sidecar 内部处理耗时（毫秒） |

---

## 4. 任务类型

### 4.1 `ping` — 心跳检测

**请求：**
```json
{ "type": "ping" }
```

**响应 result：**
```json
{ "pong": true, "sidecar_version": "0.1.0" }
```

---

### 4.2 `ocr` — 截图文字识别

**请求：**
```json
{
  "type":            "ocr",
  "capture_id":      42,
  "screenshot_path": "/path/to/user-home/.memory-bread/captures/2026/03/04/1709510400000.jpg"
}
```

**响应 result：**
```json
{
  "text":       "识别出的全文内容",
  "confidence": 0.95,
  "language":   "zh"
}
```

| 字段 | 说明 |
|------|------|
| `text` | OCR 识别的完整文字，段落间用 `\n` 分隔 |
| `confidence` | 整体置信度 0.0~1.0 |
| `language` | 检测到的主要语言，如 `"zh"`, `"en"` |

---

### 4.3 `asr` — 音频语音识别

**请求：**
```json
{
  "type":        "asr",
  "capture_id":  42,
  "audio_path":  "/path/to/user-home/.memory-bread/audio/1709510400000.wav",
  "language":    "zh"
}
```

**响应 result：**
```json
{
  "text":     "转录后的完整文字",
  "language": "zh",
  "segments": [
    { "start_sec": 0.0,  "end_sec": 2.5,  "text": "第一句话" },
    { "start_sec": 2.8,  "end_sec": 5.1,  "text": "第二句话" }
  ]
}
```

---

### 4.4 `vlm` — 视觉语言模型理解

**请求：**
```json
{
  "type":            "vlm",
  "capture_id":      42,
  "screenshot_path": "/path/to/user-home/.memory-bread/captures/2026/03/04/1709510400000.jpg",
  "prompt":          "用一句话描述用户当前正在进行什么工作"
}
```

**响应 result：**
```json
{
  "description": "用户正在飞书中撰写一份竞品分析文档",
  "scene_type":  "doc_writing",
  "tags":        ["文档", "飞书", "工作"]
}
```

| `scene_type` 枚举值 | 含义 |
|--------------------|------|
| `"doc_writing"` | 文档撰写 |
| `"im_chat"` | IM 聊天 |
| `"browsing"` | 网页浏览 |
| `"coding"` | 写代码 |
| `"spreadsheet"` | 表格操作 |
| `"video_meeting"` | 视频会议 |
| `"idle"` | 无明显工作活动 |
| `"unknown"` | 无法识别 |

---

### 4.5 `embed` — 文本向量化

**请求：**
```json
{
  "type":       "embed",
  "capture_id": 42,
  "texts":      ["第一个文本分块", "第二个文本分块"],
  "model":      "bge-m3"
}
```

**响应 result：**
```json
{
  "vectors":   [[0.021, -0.143, ...], [0.087, 0.231, ...]],
  "dimension": 1024,
  "model":     "bge-m3"
}
```

- `texts` 为数组，支持批量向量化（减少 IPC 往返次数）
- `vectors[i]` 对应 `texts[i]` 的向量

---

### 4.6 `pii_scrub` — PII 敏感信息脱敏

**请求：**
```json
{
  "type":       "pii_scrub",
  "capture_id": 42,
  "text":       "请联系张三，手机号 13800138000，邮箱 zhang@example.com"
}
```

**响应 result：**
```json
{
  "text":           "请联系[姓名]，手机号 [手机号]，邮箱 [邮箱]",
  "redacted_count": 3,
  "redacted_types": ["PERSON", "PHONE_NUMBER", "EMAIL_ADDRESS"]
}
```

---

## 5. 错误码规范

当 `status = "error"` 时，`error` 字段为如下格式的字符串：

```
{error_code}: {human_readable_message}
```

| 错误码 | 含义 |
|--------|------|
| `FILE_NOT_FOUND` | 截图/音频文件路径不存在 |
| `MODEL_NOT_LOADED` | 对应 AI 模型未加载完成 |
| `OCR_FAILED` | OCR 识别失败（图像损坏等） |
| `ASR_FAILED` | ASR 转录失败 |
| `VLM_FAILED` | VLM 推理失败 |
| `EMBED_FAILED` | 向量化失败 |
| `PII_FAILED` | 脱敏处理失败 |
| `INVALID_REQUEST` | 请求字段缺失或类型错误 |
| `INTERNAL_ERROR` | Sidecar 内部未知错误 |

---

## 6. 连接生命周期

```
Rust Engine                        Python Sidecar
    │                                    │
    │──── connect() ────────────────────>│  建立 Socket 连接
    │                                    │
    │──── Request{ping} ────────────────>│  心跳确认 Sidecar 就绪
    │<─── Response{pong} ───────────────│
    │                                    │
    │──── Request{ocr} ─────────────────>│  发送采集任务
    │<─── Response{result} ─────────────│
    │                                    │
    │──── Request{embed} ───────────────>│  可与其他请求并发
    │<─── Response{result} ─────────────│
    │                                    │
    │──── close() ──────────────────────>│  Engine 关闭连接（进程退出时）
    │                                    │
```

- Rust Engine 启动时等待 Sidecar 就绪（ping 超时重试，最多 30 秒）
- Sidecar 启动失败时，Engine 降级运行（跳过 OCR/ASR/VLM，仅保留 Accessibility Tree 采集）
