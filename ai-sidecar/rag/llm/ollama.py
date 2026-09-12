"""
Ollama 本地 LLM 后端

通过 Ollama HTTP API（localhost:11434）调用本地模型，
生成请求走统一可取消 HTTP 传输；可用性检查使用标准库 urllib。
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from typing import Callable, Optional

import httpx

from inference_queue import raise_if_preempted
from inference_transport import stream_inference_json
from runtime_endpoints import service_base_url

from .base import LlmBackend, LlmResponse

logger = logging.getLogger(__name__)


class OllamaBackend(LlmBackend):
    """Ollama 本地 LLM 后端（通过 /api/generate 调用）"""

    def __init__(
        self,
        model:       str = "qwen2.5:7b",
        base_url:    Optional[str] = None,
        timeout:     int = 60,
        num_predict: int = 1024,
    ) -> None:
        self._model       = model
        self._base_url    = (base_url or service_base_url("ollama")).rstrip("/")
        self._timeout     = timeout
        self._num_predict = num_predict

    def is_available(self) -> bool:
        """检查 Ollama 服务是否运行（访问 /api/tags 端点）"""
        try:
            req = urllib.request.Request(
                f"{self._base_url}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def complete(self, prompt: str, system: str = "", **kwargs) -> LlmResponse:
        # 即使调用方不消费增量，也使用流式传输。这样 P0 到达时可以关闭
        # 正在运行的后台 HTTP 响应，而不必等待整段非流式推理完成。
        return self.complete_stream(
            prompt,
            system=system,
            on_delta=None,
            **kwargs,
        )

    def complete_stream(
        self,
        prompt: str,
        system: str = "",
        on_delta: Optional[Callable[[str], None]] = None,
        **kwargs,
    ) -> LlmResponse:
        raise_if_preempted()
        url = f"{self._base_url}/api/generate"
        options = {
            "num_predict": kwargs.pop("num_predict", self._num_predict),
        }
        for key in ("temperature", "top_p", "seed"):
            if key in kwargs:
                options[key] = kwargs[key]

        body: dict = {
            "model": self._model,
            "prompt": prompt,
            "stream": True,
            "options": options,
            "think": False,
            "keep_alive": "10m",
        }
        if system:
            body["system"] = system

        parts: list[str] = []
        model = self._model
        tokens = 0
        done_reason = None
        def collect(payload: dict) -> None:
            nonlocal model, tokens, done_reason
            delta = payload.get("response", "")
            if delta:
                parts.append(delta)
                if on_delta:
                    on_delta(delta)
            model = payload.get("model", model)
            if payload.get("done"):
                tokens = payload.get("eval_count", 0)
                done_reason = payload.get("done_reason") or payload.get("finish_reason")

        try:
            stream_inference_json(url, body, timeout=self._timeout, on_chunk=collect)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"本地模型请求失败 ({exc.response.status_code})"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError("本地模型服务不可达") from exc

        if not done_reason and tokens >= int(options.get("num_predict", 0) or 0):
            done_reason = "length"
        return LlmResponse(
            text="".join(parts),
            model=model,
            tokens=tokens,
            done_reason=done_reason,
        )

    @property
    def model_name(self) -> str:
        return self._model
