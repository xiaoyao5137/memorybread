"""
OcrEngine 编排逻辑测试

测试覆盖：
- primary → fallback 降级逻辑
- FileNotFoundError 预检
- 后端不可用时的跳过行为
- 两个后端都失败时的 RuntimeError
- 日志与性能（大致验证）
"""

from __future__ import annotations

import importlib
import sys

import pytest

from ocr.backends.base import OcrBox, OcrOutput
from ocr.engine        import OcrEngine
from tests.conftest    import MockOcrBackend


def test_ocr_package_keeps_ipc_worker_lazy():
    import ocr

    sys.modules.pop("ocr.worker", None)
    importlib.reload(ocr)

    assert ocr.OcrEngine is OcrEngine
    assert "ocr.worker" not in sys.modules


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_output(text: str = "识别文字", conf: float = 0.9) -> OcrOutput:
    return OcrOutput(boxes=[OcrBox(text=text, confidence=conf)], language="zh")


# ── 核心流程 ──────────────────────────────────────────────────────────────────

class TestOcrEngineFlow:
    def test_uses_primary_when_available(self, jpeg_image_path):
        primary  = MockOcrBackend(output=_make_output("primary结果"))
        fallback = MockOcrBackend(output=_make_output("fallback结果"))

        engine = OcrEngine(primary=primary, fallback=fallback)
        result = engine.process(jpeg_image_path)

        assert result.text == "primary结果"
        assert primary.call_count  == 1
        assert fallback.call_count == 0     # fallback 不应被调用

    def test_falls_back_when_primary_fails(self, jpeg_image_path):
        primary  = MockOcrBackend(should_raise=RuntimeError("paddle 崩溃"))
        fallback = MockOcrBackend(output=_make_output("fallback结果"))

        engine = OcrEngine(primary=primary, fallback=fallback)
        result = engine.process(jpeg_image_path)

        assert result.text == "fallback结果"
        assert primary.call_count  == 1
        assert fallback.call_count == 1

    def test_raises_when_both_fail(self, jpeg_image_path):
        primary  = MockOcrBackend(should_raise=RuntimeError("primary 失败"))
        fallback = MockOcrBackend(should_raise=RuntimeError("fallback 失败"))

        engine = OcrEngine(primary=primary, fallback=fallback)
        with pytest.raises(RuntimeError, match="所有 OCR 后端均失败"):
            engine.process(jpeg_image_path)

    def test_skips_unavailable_primary(self, jpeg_image_path):
        primary  = MockOcrBackend(available=False)
        fallback = MockOcrBackend(output=_make_output("fallback使用"))

        engine = OcrEngine(primary=primary, fallback=fallback)
        result = engine.process(jpeg_image_path)

        assert result.text == "fallback使用"
        assert primary.call_count  == 0     # 不可用，不调用
        assert fallback.call_count == 1

    def test_raises_when_no_backends_available(self, jpeg_image_path):
        primary  = MockOcrBackend(available=False)
        fallback = MockOcrBackend(available=False)

        engine = OcrEngine(primary=primary, fallback=fallback)
        with pytest.raises(RuntimeError, match="没有可用的 OCR 后端"):
            engine.process(jpeg_image_path)

    def test_file_not_found_raises(self, nonexistent_path):
        engine = OcrEngine(
            primary=MockOcrBackend(),
            fallback=MockOcrBackend(),
        )
        with pytest.raises(FileNotFoundError, match="截图文件不存在"):
            engine.process(nonexistent_path)

    def test_file_not_found_before_backend(self, nonexistent_path):
        """文件不存在时，连后端都不应被调用"""
        primary = MockOcrBackend()
        engine  = OcrEngine(primary=primary, fallback=MockOcrBackend())
        try:
            engine.process(nonexistent_path)
        except FileNotFoundError:
            pass
        assert primary.call_count == 0      # 预检发现文件不存在，不调用后端


# ── 返回值结构 ─────────────────────────────────────────────────────────────────

class TestOcrEngineOutput:
    def test_returns_ocr_output(self, jpeg_image_path):
        engine = OcrEngine(primary=MockOcrBackend(), fallback=MockOcrBackend())
        result = engine.process(jpeg_image_path)

        assert isinstance(result, OcrOutput)
        assert isinstance(result.text, str)
        assert isinstance(result.confidence, float)
        assert isinstance(result.language, str)

    def test_multi_line_output(self, jpeg_image_path):
        output = OcrOutput(boxes=[
            OcrBox(text="行一", confidence=0.9),
            OcrBox(text="行二", confidence=0.8),
            OcrBox(text="行三", confidence=0.7),
        ])
        engine = OcrEngine(primary=MockOcrBackend(output=output), fallback=MockOcrBackend())
        result = engine.process(jpeg_image_path)

        assert result.text == "行一\n行二\n行三"
        assert result.confidence == pytest.approx(0.8)

    def test_empty_output(self, jpeg_image_path):
        engine = OcrEngine(
            primary=MockOcrBackend(output=OcrOutput(boxes=[])),
            fallback=MockOcrBackend(output=OcrOutput(boxes=[])),
        )
        result = engine.process(jpeg_image_path)

        assert result.text == ""
        assert result.is_empty
        assert result.confidence == 0.0

    def test_high_confidence_output(self, jpeg_image_path):
        output = OcrOutput(boxes=[OcrBox(text="精准识别", confidence=0.99)])
        engine = OcrEngine(primary=MockOcrBackend(output=output), fallback=MockOcrBackend())
        result = engine.process(jpeg_image_path)

        assert result.confidence == pytest.approx(0.99)


# ── 工厂方法 ──────────────────────────────────────────────────────────────────

class TestOcrEngineFactory:
    def test_create_default_returns_engine(self):
        engine = OcrEngine.create_default()
        assert isinstance(engine, OcrEngine)
        assert engine._primary  is not None
        assert engine._fallback is not None

    def test_engine_accepts_injected_backends(self):
        primary  = MockOcrBackend()
        fallback = MockOcrBackend()
        engine   = OcrEngine(primary=primary, fallback=fallback)

        assert engine._primary  is primary
        assert engine._fallback is fallback
