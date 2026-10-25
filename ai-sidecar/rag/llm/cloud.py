"""
OpenAI-compatible and Anthropic cloud LLM backend for RAG.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from .base import LlmBackend, LlmResponse

logger = logging.getLogger(__name__)


class CloudChatBackend(LlmBackend):
    """Synchronous non-streaming backend used by RAG jobs."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "",
        timeout: int = 300,
        retries: int = 2,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = (base_url or "").rstrip("/")
        self._timeout = timeout
        self._retries = max(0, retries)

    @property
    def model_name(self) -> str:
        return self._model

    def is_available(self) -> bool:
        return bool(self._model and self._api_key)

    def complete(self, prompt: str, system: str = "", **kwargs) -> LlmResponse:
        if self._model.startswith('user-'):
            from user_models import complete_profile
            return LlmResponse(text=complete_profile(self._model, prompt, system), model=self._model)
        if "claude" in self._model.lower() or "anthropic.com" in self._base_url:
            return self._complete_anthropic(prompt, system, **kwargs)
        return self._complete_openai_compatible(prompt, system, **kwargs)

    def complete_stream(
        self,
        prompt: str,
        system: str = "",
        on_delta: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> LlmResponse:
        if self._model.startswith('user-'):
            from user_models import complete_profile
            response = LlmResponse(text=complete_profile(self._model, prompt, system), model=self._model)
            if on_delta: on_delta(response.text)
            return response
        if "claude" in self._model.lower() or "anthropic.com" in self._base_url:
            return self._stream_anthropic(prompt, system, on_delta=on_delta, **kwargs)
        return self._stream_openai_compatible(prompt, system, on_delta=on_delta, **kwargs)

    def _complete_openai_compatible(self, prompt: str, system: str = "", **kwargs) -> LlmResponse:
        base_url = self._base_url or self._default_base_url()
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": kwargs.get("temperature", 0.5),
            "top_p": kwargs.get("top_p", 0.9),
            "max_tokens": kwargs.get("num_predict", 1536),
        }
        data = self._post_json(
            url,
            payload,
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}
        return LlmResponse(
            text=message.get("content", ""),
            model=data.get("model", self._model),
            tokens=usage.get("completion_tokens", 0),
            done_reason=choice.get("finish_reason"),
        )

    def _complete_anthropic(self, prompt: str, system: str = "", **kwargs) -> LlmResponse:
        url = self._anthropic_messages_url(self._base_url)
        payload = {
            "model": self._model,
            "max_tokens": kwargs.get("num_predict", 1536),
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        data = self._post_json(
            url,
            payload,
            {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )
        parts = data.get("content") or []
        text = "".join(part.get("text", "") for part in parts if part.get("type") == "text")
        usage = data.get("usage") or {}
        return LlmResponse(
            text=text,
            model=data.get("model", self._model),
            tokens=usage.get("output_tokens", 0),
            done_reason=data.get("stop_reason"),
        )

    def _stream_openai_compatible(
        self,
        prompt: str,
        system: str = "",
        on_delta: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> LlmResponse:
        base_url = self._base_url or self._default_base_url()
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self._model,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": kwargs.get("temperature", 0.5),
            "top_p": kwargs.get("top_p", 0.9),
            "max_tokens": kwargs.get("num_predict", 1536),
        }
        parts: list[str] = []
        model = self._model
        tokens = 0
        done_reason = None
        for data in self._iter_sse_json(
            url,
            payload,
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        ):
            model = data.get("model", model)
            usage = data.get("usage") or {}
            tokens = usage.get("completion_tokens", tokens)
            choice = (data.get("choices") or [{}])[0]
            delta = (choice.get("delta") or {}).get("content") or ""
            if delta:
                parts.append(delta)
                if on_delta:
                    on_delta(delta)
            done_reason = choice.get("finish_reason") or done_reason
        return LlmResponse(
            text="".join(parts),
            model=model,
            tokens=tokens,
            done_reason=done_reason,
        )

    def _stream_anthropic(
        self,
        prompt: str,
        system: str = "",
        on_delta: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> LlmResponse:
        url = self._anthropic_messages_url(self._base_url)
        payload = {
            "model": self._model,
            "max_tokens": kwargs.get("num_predict", 1536),
            "stream": True,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        parts: list[str] = []
        model = self._model
        tokens = 0
        done_reason = None
        for data in self._iter_sse_json(
            url,
            payload,
            {
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        ):
            event_type = data.get("type")
            if event_type == "message_start":
                message = data.get("message") or {}
                model = message.get("model", model)
            elif event_type == "content_block_delta":
                delta = (data.get("delta") or {}).get("text") or ""
                if delta:
                    parts.append(delta)
                    if on_delta:
                        on_delta(delta)
            elif event_type == "message_delta":
                delta = data.get("delta") or {}
                usage = data.get("usage") or {}
                tokens = usage.get("output_tokens", tokens)
                done_reason = delta.get("stop_reason") or done_reason
        return LlmResponse(
            text="".join(parts),
            model=model,
            tokens=tokens,
            done_reason=done_reason,
        )

    def _iter_sse_json(self, url: str, payload: dict, headers: dict):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if not data_text or data_text == "[DONE]":
                        continue
                    yield json.loads(data_text)
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"云端模型请求失败 HTTP {exc.code}: {body_text}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError) as exc:
            detail = self._network_error_detail(exc)
            raise RuntimeError(
                "云端模型服务不可达：网络或 TLS 连接被中断，请检查网络、代理/VPN、"
                f"模型服务 base_url 配置后重试。技术细节：{detail}"
            ) from exc

    def _post_json(self, url: str, payload: dict, headers: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        attempts = self._retries + 1
        last_exc: Optional[BaseException] = None

        for attempt in range(attempts):
            req = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body_text = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"云端模型请求失败 HTTP {exc.code}: {body_text}") from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError) as exc:
                last_exc = exc
                if attempt >= self._retries or not self._is_retryable_network_error(exc):
                    break
                delay = min(0.4 * (2 ** attempt), 2.0)
                logger.warning(
                    "云端模型请求网络中断，准备重试 %s/%s: %s",
                    attempt + 1,
                    self._retries,
                    self._network_error_detail(exc),
                )
                time.sleep(delay)

        detail = self._network_error_detail(last_exc)
        raise RuntimeError(
            "云端模型服务不可达：网络或 TLS 连接被中断，请检查网络、代理/VPN、"
            f"模型服务 base_url 配置后重试。技术细节：{detail}"
        ) from last_exc

    @staticmethod
    def _is_retryable_network_error(exc: BaseException) -> bool:
        if isinstance(exc, (TimeoutError, socket.timeout, ssl.SSLError, ConnectionResetError, ConnectionAbortedError)):
            return True
        if isinstance(exc, urllib.error.URLError):
            reason = exc.reason
            if isinstance(reason, (TimeoutError, socket.timeout, ssl.SSLError, ConnectionError, OSError)):
                return True
            text = str(reason).lower()
        else:
            text = str(exc).lower()
        retry_markers = (
            "unexpected_eof_while_reading",
            "eof occurred in violation of protocol",
            "connection reset",
            "connection aborted",
            "timed out",
            "temporarily unavailable",
        )
        return any(marker in text for marker in retry_markers)

    @staticmethod
    def _network_error_detail(exc: Optional[BaseException]) -> str:
        if exc is None:
            return "unknown network error"
        if isinstance(exc, urllib.error.URLError):
            return str(exc.reason)
        return str(exc)

    def _default_base_url(self) -> str:
        lower = self._model.lower()
        if "qwen" in lower:
            return "https://dashscope.aliyuncs.com/compatible-mode/v1"
        if "glm" in lower:
            return "https://open.bigmodel.cn/api/paas/v4"
        if "moonshot" in lower or "kimi" in lower:
            return "https://api.moonshot.cn/v1"
        return "https://api.openai.com/v1"

    @staticmethod
    def _anthropic_messages_url(base_url: str) -> str:
        base = (base_url or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"
