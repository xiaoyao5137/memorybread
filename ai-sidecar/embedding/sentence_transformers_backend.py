"""
SentenceTransformers Embedding 后端

当 Ollama 不可用时（如 Ollama 0.30.x 移除 llama-server 导致 GGUF 模型失效），
使用 sentence-transformers 直接在进程内加载模型，作为 fallback。

加载策略（离线优先）：先通过 model_sources 解析本地模型目录（应用自有目录
或 huggingface 缓存），再以 local_files_only 方式加载，避免断网环境下
huggingface.co 在线校验拖慢启动。模型缺失时才走境内镜像级联下载。
"""

from __future__ import annotations

import logging
from .base import EmbeddingBackend, EmbeddingVector
from .model_sources import build_local_files_only_kwargs, resolve_embedding_model_source

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
_DEFAULT_DIMENSION = 512


class SentenceTransformersBackend(EmbeddingBackend):
    """sentence-transformers 本地推理后端（无需 Ollama）"""

    def __init__(self, model_name: str = _DEFAULT_MODEL) -> None:
        self._model_name = model_name
        self._model = None

    def is_available(self) -> bool:
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            model_source = resolve_embedding_model_source()
            logger.info("加载本地 embedding 模型: %s (源: %s)", self._model_name, model_source)
            kwargs = build_local_files_only_kwargs()
            try:
                self._model = SentenceTransformer(model_source, **kwargs)
            except TypeError:
                # 旧版 sentence-transformers 不识别 local_files_only，
                # 传入的是本地目录，去掉参数重试仍然不会联网。
                self._model = SentenceTransformer(model_source)
        return self._model

    def encode(self, texts: list[str]) -> list[EmbeddingVector]:
        valid = [t for t in texts if t and t.strip()]
        if not valid:
            return []
        model = self._load()
        vecs = model.encode(valid, normalize_embeddings=True).tolist()
        return [EmbeddingVector(text=t, vector=v) for t, v in zip(valid, vecs)]

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return _DEFAULT_DIMENSION
