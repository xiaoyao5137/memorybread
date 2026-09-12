"""
LLM 用量埋点工具

所有调用 LLM 的模块（TaskExecutor、RAG、KnowledgeExtractor）
在调用后通过此模块记录 token 用量到 llm_usage_logs 表。
"""

from __future__ import annotations

import sqlite3
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = str(Path.home() / ".memory-bread" / "memory-bread.db")

SQLITE_MAX_INTEGER = 2**63 - 1


def _usage_integer(value, fallback=0):
    """Usage columns accept bounded integers, never arbitrary model metadata."""
    return value if type(value) is int and 0 <= value <= SQLITE_MAX_INTEGER else fallback


def log_llm_usage(
    caller: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    caller_id: Optional[str] = None,
    status: str = "success",
    error_msg: Optional[str] = None,
    raw_preview: Optional[str] = None,
    response_preview: Optional[str] = None,
    done_reason: Optional[str] = None,
    db_path: str = DB_PATH,
):
    """
    记录一次 LLM 调用的 token 用量。

    Args:
        caller: 调用来源，'rag' | 'task' | 'knowledge'
        model_name: 模型名称，如 'qwen2.5:3b'
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        latency_ms: 调用耗时（毫秒）
        caller_id: 关联 ID（task_id / rag_session_id 等）
        status: 'success' | 'failed'
        error_msg: 失败原因
    """
    try:
        prompt_tokens = _usage_integer(prompt_tokens)
        completion_tokens = _usage_integer(completion_tokens)
        latency_ms = _usage_integer(latency_ms)
        conn = sqlite3.connect(db_path)
        _ensure_llm_usage_trace_columns(conn)
        conn.execute(
            """INSERT INTO llm_usage_logs
               (ts, caller, caller_id, model_name, prompt_tokens, completion_tokens,
                total_tokens, latency_ms, status, error_msg, raw_preview, response_preview, done_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(time.time() * 1000),
                caller,
                caller_id,
                model_name,
                prompt_tokens,
                completion_tokens,
                min(prompt_tokens + completion_tokens, SQLITE_MAX_INTEGER),
                latency_ms,
                status,
                error_msg,
                _truncate_trace(raw_preview),
                _truncate_trace(response_preview),
                done_reason,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        # 埋点失败不影响主流程
        logger.warning("LLM 用量埋点失败 code=LLM_USAGE_WRITE_FAILED")


def _truncate_trace(value: Optional[str], limit: int = 4000) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...(truncated)"


def _ensure_llm_usage_trace_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage_logs)")}
    columns = {
        "raw_preview": "TEXT",
        "response_preview": "TEXT",
        "done_reason": "TEXT",
    }
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE llm_usage_logs ADD COLUMN {name} {definition}")


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中文约1.5字/token，英文约4字/token）"""
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


class LLMCallTracker:
    """
    上下文管理器，自动记录 LLM 调用的耗时和 token 用量。

    用法：
        with LLMCallTracker(caller='rag', model='qwen3.5:4b') as tracker:
            response = client.chat(...)
            tracker.set_response(response)
    """

    def __init__(self, caller: str, model_name: str, caller_id: Optional[str] = None, db_path: str = DB_PATH, *, capture_content: bool = True):
        self.capture_content = capture_content
        self.caller = caller
        self.model_name = model_name
        self.caller_id = caller_id
        self.db_path = db_path
        self._start_ms = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._status = "success"
        self._error_msg = None
        self._raw_preview = None
        self._response_preview = None
        self._done_reason = None

    def __enter__(self):
        self._start_ms = int(time.time() * 1000)
        return self

    def set_response(self, response: dict):
        """从 Ollama 响应中提取 token 用量"""
        usage = response.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        # Ollama 响应格式
        self._prompt_tokens = (
            _usage_integer(usage.get("prompt_tokens"))
            or _usage_integer(response.get("prompt_eval_count"))
            or 0
        )
        self._completion_tokens = (
            _usage_integer(usage.get("completion_tokens"))
            or _usage_integer(response.get("eval_count"))
            or 0
        )
        # 如果没有 token 信息，用文本估算
        if self._prompt_tokens == 0:
            msg = response.get("message", {})
            if not isinstance(msg, dict):
                msg = {}
            content = msg.get("content", "")
            # Qwen3.5 等推理模型可能将内容放在 thinking 字段
            if not content:
                content = msg.get("thinking", "")
            self._completion_tokens = estimate_tokens(content if isinstance(content, str) else '')
        self._done_reason = response.get("done_reason")

    def set_trace(
        self,
        raw_preview: Optional[str] = None,
        response_preview: Optional[str] = None,
        done_reason: Optional[str] = None,
    ):
        self._raw_preview = raw_preview if self.capture_content else None
        self._response_preview = response_preview if self.capture_content else None
        if done_reason:
            self._done_reason = done_reason

    def set_error(self, error_msg: str):
        self._status = "failed"
        self._error_msg = error_msg if self.capture_content else "INFERENCE_FAILED"

    def set_tokens(self, prompt: int, completion: int):
        self._prompt_tokens = _usage_integer(prompt)
        self._completion_tokens = _usage_integer(completion)

    def __exit__(self, exc_type, exc_val, exc_tb):
        latency_ms = int(time.time() * 1000) - self._start_ms
        if exc_type is not None:
            self._status = "failed"
            self._error_msg = str(exc_val) if self.capture_content else "INFERENCE_FAILED"
        log_llm_usage(
            caller=self.caller,
            model_name=self.model_name,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            latency_ms=latency_ms,
            caller_id=self.caller_id,
            status=self._status,
            error_msg=self._error_msg,
            raw_preview=self._raw_preview,
            response_preview=self._response_preview,
            done_reason=(self._done_reason if self.capture_content or (isinstance(self._done_reason, str) and self._done_reason in {"stop", "length", "repetition", "cancelled"}) else None),
            db_path=self.db_path,
        )
        return False  # 不吞异常
