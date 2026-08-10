"""
Embedding 模块测试

测试覆盖：
- EmbeddingVector 数据类属性
- MockEmbeddingBackend 行为
- EmbeddingModel 编排逻辑（encode / 错误处理 / model_name / dimension）
- EmbedWorker 异步 IPC 处理（成功 / 空文本 / 后端不可用 / 意外异常）
- SentenceTransformersBackend 接口（不加载实际模型）
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from embedding.base   import EmbeddingBackend, EmbeddingVector
from embedding.model  import EmbeddingModel
from embedding.sentence_transformers_backend import SentenceTransformersBackend
from embedding.worker import EmbedWorker
from memory_bread_ipc    import IpcResponse, ResponseStatus


# ── Mock 后端 ─────────────────────────────────────────────────────────────────

class MockEmbeddingBackend(EmbeddingBackend):
    """测试用 Mock Embedding 后端，返回固定维度零向量"""

    def __init__(
        self,
        dim:          int                = 4,
        available:    bool               = True,
        should_raise: Exception | None   = None,
    ) -> None:
        self._dim          = dim
        self._available    = available
        self._should_raise = should_raise
        self.call_count    = 0

    def is_available(self) -> bool:
        return self._available

    def encode(self, texts: list[str]) -> list[EmbeddingVector]:
        self.call_count += 1
        if self._should_raise:
            raise self._should_raise
        return [
            EmbeddingVector(text=t, vector=[float(i + 1) * 0.1] * self._dim)
            for i, t in enumerate(texts)
        ]

    @property
    def model_name(self) -> str:
        return "mock-embedding"

    @property
    def dimension(self) -> int:
        return self._dim


# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _make_embed_request(texts: list[str]):
    import time
    from memory_bread_ipc import IpcRequest
    from memory_bread_ipc.message import EmbedRequest
    task = EmbedRequest(capture_id=1, texts=texts, model="bge-m3")
    return IpcRequest(id=str(uuid.uuid4()), ts=int(time.time() * 1000), task=task)


# ── EmbeddingVector ───────────────────────────────────────────────────────────

class TestEmbeddingVector:
    def test_dimension(self):
        v = EmbeddingVector(text="hello", vector=[0.1, 0.2, 0.3])
        assert v.dimension == 3

    def test_empty_vector(self):
        v = EmbeddingVector(text="", vector=[])
        assert v.dimension == 0

    def test_text_preserved(self):
        v = EmbeddingVector(text="工作记录", vector=[1.0, 2.0])
        assert v.text == "工作记录"

    def test_large_vector(self):
        v = EmbeddingVector(text="x", vector=[0.0] * 1024)
        assert v.dimension == 1024


# ── MockEmbeddingBackend ──────────────────────────────────────────────────────

class TestMockEmbeddingBackend:
    def test_encode_returns_vectors(self):
        backend = MockEmbeddingBackend(dim=4)
        results = backend.encode(["hello", "world"])
        assert len(results) == 2
        assert all(len(r.vector) == 4 for r in results)

    def test_encode_preserves_text(self):
        backend = MockEmbeddingBackend()
        results = backend.encode(["测试文字"])
        assert results[0].text == "测试文字"

    def test_encode_empty_input(self):
        backend = MockEmbeddingBackend()
        results = backend.encode([])
        assert results == []

    def test_raises_when_configured(self):
        backend = MockEmbeddingBackend(should_raise=RuntimeError("模型崩溃"))
        with pytest.raises(RuntimeError, match="模型崩溃"):
            backend.encode(["text"])

    def test_unavailable(self):
        backend = MockEmbeddingBackend(available=False)
        assert not backend.is_available()

    def test_call_count(self):
        backend = MockEmbeddingBackend()
        backend.encode(["a"])
        backend.encode(["b", "c"])
        assert backend.call_count == 2

    def test_is_backend_subclass(self):
        assert isinstance(MockEmbeddingBackend(), EmbeddingBackend)


# ── EmbeddingModel ────────────────────────────────────────────────────────────

class TestEmbeddingModel:
    def test_encode_success(self):
        model   = EmbeddingModel(backend=MockEmbeddingBackend(dim=8))
        results = model.encode(["工作日志", "会议记录"])
        assert len(results) == 2
        assert all(len(r.vector) == 8 for r in results)

    def test_encode_empty_list(self):
        model   = EmbeddingModel(backend=MockEmbeddingBackend())
        results = model.encode([])
        assert results == []

    def test_raises_when_backend_unavailable(self):
        model = EmbeddingModel(backend=MockEmbeddingBackend(available=False))
        with pytest.raises(RuntimeError, match="不可用"):
            model.encode(["text"])

    def test_model_name(self):
        model = EmbeddingModel(backend=MockEmbeddingBackend())
        assert model.model_name == "mock-embedding"

    def test_dimension(self):
        model = EmbeddingModel(backend=MockEmbeddingBackend(dim=1024))
        assert model.dimension == 1024

    def test_text_preserved_in_result(self):
        model   = EmbeddingModel(backend=MockEmbeddingBackend())
        results = model.encode(["测试文本"])
        assert results[0].text == "测试文本"

    def test_create_default_returns_model(self):
        model = EmbeddingModel.create_default()
        assert isinstance(model, EmbeddingModel)
        assert model.model_name is not None

    def test_backend_called_once(self):
        backend = MockEmbeddingBackend()
        model   = EmbeddingModel(backend=backend)
        model.encode(["a", "b", "c"])
        assert backend.call_count == 1


# ── EmbedWorker ───────────────────────────────────────────────────────────────

class TestEmbedWorkerSuccess:
    async def test_ok_response(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend(dim=4))
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["hello", "world"])

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.OK
        assert resp.id == req.id
        assert resp.result is not None

    async def test_vectors_count_matches_input(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend(dim=4))
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["文本一", "文本二", "文本三"])

        resp = await worker.handle(req)

        assert len(resp.result.vectors) == 3

    async def test_dimension_in_result(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend(dim=8))
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["测试"])

        resp = await worker.handle(req)

        assert resp.result.dimension == 8

    async def test_model_name_in_result(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend())
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["文本"])

        resp = await worker.handle(req)

        assert resp.result.model == "mock-embedding"

    async def test_latency_recorded(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend())
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["文本"])

        resp = await worker.handle(req)
        assert resp.latency_ms >= 0

    async def test_response_id_matches(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend())
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["文本"])

        resp = await worker.handle(req)
        assert resp.id == req.id

    async def test_empty_texts_returns_ok(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend())
        worker = EmbedWorker(model=model)
        req    = _make_embed_request([])

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.OK
        assert resp.result.vectors == []


class TestEmbedWorkerErrors:
    async def test_backend_unavailable_returns_error(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend(available=False))
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["text"])

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert "EMBED_FAILED" in (resp.error or "")

    async def test_runtime_error_returns_embed_failed(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend(
            available=True, should_raise=RuntimeError("模型崩溃")
        ))
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["text"])

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert "EMBED_FAILED" in (resp.error or "")

    async def test_unexpected_exception_returns_internal_error(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend(
            available=True, should_raise=ValueError("意外")
        ))
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["text"])

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR

    async def test_error_response_has_id(self):
        model  = EmbeddingModel(backend=MockEmbeddingBackend(available=False))
        worker = EmbedWorker(model=model)
        req    = _make_embed_request(["text"])

        resp = await worker.handle(req)
        assert resp.id == req.id


# ── SentenceTransformersBackend 接口测试 ─────────────────────────────────────

class TestSentenceTransformersBackend:
    def test_model_name_default(self):
        backend = SentenceTransformersBackend()
        assert "bge-small-zh" in backend.model_name.lower()

    def test_dimension_default_before_loading(self):
        backend = SentenceTransformersBackend()
        assert backend.dimension == 512

    def test_lazy_load(self):
        """初始化时不应加载模型"""
        backend = SentenceTransformersBackend()
        assert backend._model is None

    def test_is_available_returns_bool(self):
        backend = SentenceTransformersBackend()
        assert isinstance(backend.is_available(), bool)

    def test_empty_input_does_not_load_model(self):
        backend = SentenceTransformersBackend()
        assert backend.encode([]) == []
        assert backend._model is None
