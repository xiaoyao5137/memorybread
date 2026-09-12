"""
EmbeddingModel — Embedding 编排器

提供统一的 encode() 接口，封装后端选择逻辑。
支持依赖注入（测试时注入 MockEmbeddingBackend）。
"""

from __future__ import annotations

import logging
from .base import EmbeddingBackend, EmbeddingVector
from .sentence_transformers_backend import SentenceTransformersBackend

logger = logging.getLogger(__name__)

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

        向量能力只使用 sentence-transformers CPU 后端。初始化负责下载并
        校验唯一的固定版本，不再回退到 Ollama 的第二套向量模型。
        """
        st = SentenceTransformersBackend()
        if not st.is_available():
            raise RuntimeError("内置向量运行时不可用，请重新安装最新版应用")
        logger.info("使用唯一的 sentence-transformers CPU embedding 后端")
        return cls(backend=st)

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
                "（请确认内置向量组件完整）"
            )
        return self._backend.encode(texts)

    @property
    def model_name(self) -> str:
        return self._backend.model_name

    @property
    def dimension(self) -> int:
        return self._backend.dimension
