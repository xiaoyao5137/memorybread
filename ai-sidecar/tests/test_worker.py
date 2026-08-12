"""
OcrWorker IPC 集成测试

测试覆盖：
- 正常 OCR 成功流程（IpcRequest → IpcResponse.ok）
- 文件不存在返回错误响应（不抛异常）
- OCR 引擎失败返回错误响应
- 响应结构校验（id、status、latency_ms、result 字段）
- 并发处理（多个请求不互相干扰）
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Optional

import pytest

from ocr.backends.base import OcrBox, OcrOutput
from ocr.engine        import OcrEngine
from ocr.worker        import OcrWorker
from tests.conftest    import MockOcrBackend
from memory_bread_ipc     import IpcResponse, ResponseStatus


# ── 辅助 ─────────────────────────────────────────────────────────────────────

def _make_engine_with(
    text:         str  = "识别文字",
    conf:         float = 0.9,
    should_raise: Optional[Exception] = None,
    available:    bool = True,
) -> OcrEngine:
    mock = MockOcrBackend(
        output=OcrOutput(boxes=[OcrBox(text=text, confidence=conf)]),
        should_raise=should_raise,
        available=available,
    )
    return OcrEngine(primary=mock, fallback=MockOcrBackend(available=False))


# ── 成功路径 ──────────────────────────────────────────────────────────────────

class TestOcrWorkerSuccess:
    async def test_ok_response_on_success(self, jpeg_image_path, make_ocr_request):
        engine = _make_engine_with(text="成功文字", conf=0.95)
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=jpeg_image_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.OK
        assert resp.id == req.id
        assert resp.error is None
        assert resp.result is not None

    async def test_result_contains_text(self, jpeg_image_path, make_ocr_request):
        engine = _make_engine_with(text="工作记录内容", conf=0.88)
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=jpeg_image_path)

        resp = await worker.handle(req)
        result = resp.result

        assert result.text == "工作记录内容"

    def test_result_confidence_rounded(self, jpeg_image_path, make_ocr_request):
        """置信度应保留 4 位小数"""
        engine = _make_engine_with(conf=0.876543)
        worker = OcrWorker(engine=engine)

        async def _run():
            req = make_ocr_request(screenshot_path=jpeg_image_path)
            return await worker.handle(req)

        resp = asyncio.run(_run())
        assert abs(resp.result.confidence - round(0.876543, 4)) < 1e-6

    async def test_latency_recorded(self, jpeg_image_path, make_ocr_request):
        engine = _make_engine_with()
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=jpeg_image_path)

        resp = await worker.handle(req)
        assert resp.latency_ms >= 0

    async def test_response_id_matches_request(self, jpeg_image_path, make_ocr_request):
        engine = _make_engine_with()
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=jpeg_image_path)

        resp = await worker.handle(req)
        assert resp.id == req.id

    async def test_language_in_result(self, jpeg_image_path, make_ocr_request):
        engine = _make_engine_with()
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=jpeg_image_path)

        resp = await worker.handle(req)
        assert resp.result.language in ("zh", "en", "ch")


# ── 错误路径 ──────────────────────────────────────────────────────────────────

class TestOcrWorkerErrors:
    async def test_file_not_found_returns_error(self, nonexistent_path, make_ocr_request):
        engine = _make_engine_with()
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=nonexistent_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert resp.id == req.id
        assert "FILE_NOT_FOUND" in (resp.error or "")

    async def test_engine_failure_returns_error(self, jpeg_image_path, make_ocr_request):
        engine = _make_engine_with(should_raise=RuntimeError("PaddleOCR 崩溃"))
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=jpeg_image_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        assert "OCR_FAILED" in (resp.error or "")

    async def test_unknown_exception_returns_internal_error(
        self, jpeg_image_path, make_ocr_request
    ):
        engine = _make_engine_with(should_raise=ValueError("意外错误"))
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=jpeg_image_path)

        resp = await worker.handle(req)

        assert resp.status == ResponseStatus.ERROR
        # 应捕获所有异常，不向上抛出

    async def test_error_response_has_id(self, nonexistent_path, make_ocr_request):
        """错误响应也必须包含正确的请求 ID"""
        engine = _make_engine_with()
        worker = OcrWorker(engine=engine)
        req    = make_ocr_request(screenshot_path=nonexistent_path)

        resp = await worker.handle(req)
        assert resp.id == req.id


# ── 并发安全 ──────────────────────────────────────────────────────────────────

class TestOcrWorkerConcurrency:
    async def test_concurrent_requests(self, jpeg_image_path, make_ocr_request):
        """多个并发请求应全部独立完成，互不干扰"""
        engine  = _make_engine_with(text="并发文字", conf=0.9)
        worker  = OcrWorker(engine=engine)
        n_tasks = 5

        tasks = [
            worker.handle(make_ocr_request(screenshot_path=jpeg_image_path))
            for _ in range(n_tasks)
        ]
        responses = await asyncio.gather(*tasks)

        assert len(responses) == n_tasks
        assert all(r.status == ResponseStatus.OK for r in responses)
        # 每次调用 make_ocr_request 生成新 UUID，所以 n_tasks 个响应有 n_tasks 个不同 ID
        assert len({r.id for r in responses}) == n_tasks

    async def test_ocr_engine_process_is_strictly_serial(
        self, jpeg_image_path, make_ocr_request
    ):
        class ConcurrencyTrackingBackend:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self.active = 0
                self.max_active = 0

            def is_available(self) -> bool:
                return True

            def run(self, _image_path: str) -> OcrOutput:
                with self._lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.03)
                with self._lock:
                    self.active -= 1
                return OcrOutput(boxes=[OcrBox(text="串行识别", confidence=0.93)])

        backend = ConcurrencyTrackingBackend()
        worker = OcrWorker(
            engine=OcrEngine(primary=backend, fallback=MockOcrBackend(available=False))
        )
        responses = await asyncio.gather(
            *[
                worker.handle(make_ocr_request(screenshot_path=jpeg_image_path))
                for _ in range(5)
            ]
        )

        assert all(response.status == ResponseStatus.OK for response in responses)
        assert backend.max_active == 1

    async def test_concurrent_requests_share_single_lazy_init(self, jpeg_image_path, make_ocr_request):
        class ThreadSafeLazyBackend:
            def __init__(self) -> None:
                self._loaded = False
                self._load_lock = threading.Lock()
                self.load_count = 0

            def is_available(self) -> bool:
                return True

            def run(self, image_path: str) -> OcrOutput:
                if not self._loaded:
                    with self._load_lock:
                        if not self._loaded:
                            time.sleep(0.02)
                            self.load_count += 1
                            self._loaded = True
                return OcrOutput(boxes=[OcrBox(text="延迟初始化", confidence=0.91)])

        backend = ThreadSafeLazyBackend()
        worker = OcrWorker(engine=OcrEngine(primary=backend, fallback=MockOcrBackend(available=False)))

        tasks = [
            worker.handle(make_ocr_request(screenshot_path=jpeg_image_path))
            for _ in range(5)
        ]
        responses = await asyncio.gather(*tasks)

        assert all(r.status == ResponseStatus.OK for r in responses)
        assert all(r.result.text == "延迟初始化" for r in responses)
        assert backend.load_count == 1
