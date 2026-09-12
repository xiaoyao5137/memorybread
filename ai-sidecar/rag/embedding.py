"""
Embedding 向量化服务

使用唯一的本地 SentenceTransformers CPU 后端将文本转换为向量。
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)


class EmbeddingService:
    """文本向量化服务（固定版本的本地 CPU 模型）。"""

    def __init__(self):
        """
        初始化 Embedding 服务

        Args:
            模型由初始化器按统一能力版本准备，不接受运行时切换。
        """
        from embedding.model_sources import MODEL_CAPABILITY_ID

        self.model_name = MODEL_CAPABILITY_ID
        self._model = None
        logger.info("初始化 EmbeddingService，能力版本: %s", self.model_name)

    def load_model(self):
        """延迟加载唯一的 SentenceTransformers 后端。"""
        if self._model is None:
            from embedding.sentence_transformers_backend import SentenceTransformersBackend
            self._model = SentenceTransformersBackend()
            logger.info("本地 CPU Embedding 后端就绪")

    def encode(self, texts: List[str]) -> List[List[float]]:
        """
        将文本列表转换为向量

        Args:
            texts: 文本列表

        Returns:
            向量列表，每个向量是一个浮点数列表
        """
        self.load_model()

        if not texts:
            return []

        logger.debug(f"正在向量化 {len(texts)} 条文本")
        results = self._model.encode(texts)
        vectors = [vec.vector for vec in results]

        if vectors:
            logger.debug(f"向量化完成，维度: {len(vectors[0])}")
        return vectors

    def encode_single(self, text: str) -> List[float]:
        """
        向量化单个文本

        Args:
            text: 单个文本

        Returns:
            向量（浮点数列表）
        """
        vectors = self.encode([text])
        return vectors[0] if vectors else []


# 全局单例
_embedding_service = None


def get_embedding_service() -> EmbeddingService:
    """获取全局 Embedding 服务单例"""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService()
    return _embedding_service
