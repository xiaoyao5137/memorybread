"""
Ollama Embedding 后端

使用 Ollama API 进行 embedding，支持量化模型（Q4/Q8）。
优势：统一进程、自动量化、内存占用低（~100MB）。
"""

from __future__ import annotations

import logging
import urllib.request
import urllib.error
import json
from typing import Optional

from .base import EmbeddingBackend, EmbeddingVector
from runtime_endpoints import service_base_url

logger = logging.getLogger(__name__)

_DEFAULT_DIMENSION = 512


class OllamaEmbeddingBackend(EmbeddingBackend):
    """
    Ollama Embedding 后端。

    使用 Ollama API 进行 embedding，支持量化模型。
    内存占用：~100MB（vs PyTorch 4.7GB）
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        api_url: Optional[str] = None,
        timeout: int = 30,
    ) -> None:
        if not model_name:
            raise RuntimeError("Ollama 向量后端已停用，请使用本地 CPU 向量能力")
        self._model_name = model_name
        self._api_url = api_url or (service_base_url("ollama") + "/api/embed")
        self._timeout = timeout

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(service_base_url("ollama") + "/api/tags")
            with urllib.request.urlopen(req, timeout=1) as resp:
                return resp.status == 200
        except Exception:
            return False

    def encode(self, texts: list[str]) -> list[EmbeddingVector]:
        if not texts:
            return []

        valid_texts = [t for t in texts if t and t.strip()]
        if not valid_texts:
            return []

        try:
            payload = {
                "model": self._model_name,
                "input": valid_texts,
                # keep_alive=30m：Embedding 模型调用频繁，保留 30 分钟避免反复加载
                "keep_alive": "30m",
            }
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                self._api_url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                result = json.loads(resp.read().decode())
                embeddings = result["embeddings"]
                return [
                    EmbeddingVector(text=text, vector=vec)
                    for text, vec in zip(valid_texts, embeddings)
                ]
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            logger.error(f"Ollama API 调用失败: {e}, 响应: {error_body[:200]}")
            raise RuntimeError(f"Ollama embedding 失败: {e}") from e
        except urllib.error.URLError as e:
            logger.error("Ollama API 调用失败: %s", e)
            raise RuntimeError(f"Ollama embedding 失败: {e}") from e

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return _DEFAULT_DIMENSION
