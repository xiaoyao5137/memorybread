"""
EmbeddingModel — Embedding 编排器

提供统一的 encode() 接口，封装后端选择逻辑。
支持依赖注入（测试时注入 MockEmbeddingBackend）。
"""

from __future__ import annotations

import logging
import os

from .base import EmbeddingBackend, EmbeddingVector
from .ollama import OllamaEmbeddingBackend
from .sentence_transformers_backend import SentenceTransformersBackend

logger = logging.getLogger(__name__)

# 允许通过环境变量强制指定后端（"st" / "ollama"）；默认 st。
_EMBEDDING_BACKEND_ENV = "MEMORYBREAD_EMBEDDING_BACKEND"


class EmbeddingModel:
    """
    Embedding 模型编排器。

    默认使用 SentenceTransformersBackend（进程内 CPU 推理，bge-small-zh-v1.5），
    可通过构造函数注入自定义后端。
    """

    def __init__(self, backend: Optional[EmbeddingBackend] = None) -> None:
        self._backend = backend or SentenceTransformersBackend()

    # ── 工厂方法 ──────────────────────────────────────────────────────────────

    @classmethod
    def create_default(cls) -> "EmbeddingModel":
        """创建默认配置的 EmbeddingModel。

        优先 sentence-transformers：进程内推理不占用推理运行时，避免
        Ollama 额外拉起一个 embedding 专用 llama-server（全局只允许存在
        一个 llama-server，防止模型驻留内存翻倍与孤儿进程泄漏）。
        ST 不可用时才降级到 Ollama 后端。
        """
        forced = os.environ.get(_EMBEDDING_BACKEND_ENV, "").strip().lower()
        if forced == "ollama":
            logger.info(
                "按 %s=ollama 强制使用 Ollama embedding 后端", _EMBEDDING_BACKEND_ENV
            )
            return cls(backend=OllamaEmbeddingBackend())

        st = SentenceTransformersBackend()
        if st.is_available():
            logger.info("使用 sentence-transformers 本地 embedding 后端")
            return cls(backend=st)
        logger.warning("sentence-transformers 不可用，降级到 Ollama embedding 后端")
        return cls(backend=OllamaEmbeddingBackend())

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def encode(self, texts: list[str]) -> list[EmbeddingVector]:
        """
        将文本列表编码为 Embedding 向量。

        Raises:
            RuntimeError: 后端不可用或编码过程中出现错误
        """
        if not texts:
            return []
        if not self._backend.is_available():
            raise RuntimeError(
                f"Embedding 后端 {self._backend.model_name!r} 不可用"
                "（请确认 Ollama 正在运行，且该 embedding 模型已安装）"
            )
        return self._backend.encode(texts)

    @property
    def model_name(self) -> str:
        return self._backend.model_name

    @property
    def dimension(self) -> int:
        return self._backend.dimension
