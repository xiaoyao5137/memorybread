"""
后端数据类型与接口测试

测试覆盖：
- OcrBox / OcrOutput 数据类属性
- PaddleBackend.is_available()（不依赖实际安装）
- AppleVisionBackend 平台检测
- MockOcrBackend 各种配置
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from ocr.backends.base   import OcrBackend, OcrBox, OcrOutput
from ocr.backends.paddle import PaddleBackend
from ocr.backends.vision import AppleVisionBackend
from ocr.backends.vision_pyobjc import AppleVisionBackend as PyObjCAppleVisionBackend
from tests.conftest      import MockOcrBackend


# ─────────────────────────────────────────────────────────────────────────────
# OcrBox 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestOcrBox:
    def test_basic_fields(self):
        box = OcrBox(text="Hello", confidence=0.9, bbox=[[0,0],[10,0],[10,5],[0,5]])
        assert box.text == "Hello"
        assert box.confidence == pytest.approx(0.9)
        assert len(box.bbox) == 4

    def test_default_bbox(self):
        box = OcrBox(text="X", confidence=1.0)
        assert box.bbox == []

    def test_chinese_text(self):
        box = OcrBox(text="你好世界", confidence=0.88)
        assert box.text == "你好世界"


# ─────────────────────────────────────────────────────────────────────────────
# OcrOutput 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestOcrOutput:
    def test_text_joins_lines(self):
        output = OcrOutput(boxes=[
            OcrBox(text="第一行", confidence=0.9),
            OcrBox(text="第二行", confidence=0.8),
        ])
        assert output.text == "第一行\n第二行"

    def test_text_skips_empty(self):
        output = OcrOutput(boxes=[
            OcrBox(text="有效行", confidence=0.9),
            OcrBox(text="   ",   confidence=0.1),   # 纯空格，应被过滤
            OcrBox(text="",      confidence=0.0),    # 空字符串
        ])
        assert output.text == "有效行"

    def test_confidence_average(self):
        output = OcrOutput(boxes=[
            OcrBox(text="A", confidence=0.8),
            OcrBox(text="B", confidence=0.6),
        ])
        assert output.confidence == pytest.approx(0.7)

    def test_confidence_empty(self):
        assert OcrOutput(boxes=[]).confidence == 0.0

    def test_is_empty_true(self):
        assert OcrOutput(boxes=[]).is_empty
        assert OcrOutput(boxes=[OcrBox(text="  ", confidence=0.5)]).is_empty

    def test_is_empty_false(self):
        assert not OcrOutput(boxes=[OcrBox(text="文字", confidence=0.9)]).is_empty

    def test_language_default(self):
        output = OcrOutput(boxes=[])
        assert output.language == "zh"

    def test_single_box(self):
        output = OcrOutput(boxes=[OcrBox(text="单行文字", confidence=1.0)])
        assert output.text == "单行文字"
        assert output.confidence == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────────────────
# PaddleBackend 测试（不需要安装 paddleocr）
# ─────────────────────────────────────────────────────────────────────────────

class TestPaddleBackend:
    def test_is_available_without_paddle(self):
        """在没有安装 paddleocr 的环境（如 CI）中，is_available 应返回 False"""
        backend = PaddleBackend()
        # 结果取决于环境，但不应抛异常
        result = backend.is_available()
        assert isinstance(result, bool)

    def test_run_raises_when_not_installed(self):
        """未安装 paddleocr 时，run() 应抛出 RuntimeError"""
        try:
            import paddleocr  # type: ignore  # noqa: F401
            pytest.skip("paddleocr 已安装，跳过此测试")
        except ImportError:
            pass

        backend = PaddleBackend()
        with pytest.raises(RuntimeError, match="paddleocr 未安装"):
            backend.run("/tmp/dummy.jpg")

    def test_default_language(self):
        backend = PaddleBackend()
        assert backend._lang == "ch"

    def test_custom_language(self):
        backend = PaddleBackend(lang="en")
        assert backend._lang == "en"

    def test_lazy_load(self):
        """初始化时不应立即加载模型（_ocr 应为 None）"""
        backend = PaddleBackend()
        assert backend._ocr is None

    def test_concurrent_init_only_loads_once(self, monkeypatch):
        init_count = 0
        init_count_lock = threading.Lock()
        start_event = threading.Event()

        class FakePaddleOCR:
            def __init__(self, **kwargs):
                nonlocal init_count
                start_event.wait(timeout=1)
                time.sleep(0.02)
                with init_count_lock:
                    init_count += 1

            def ocr(self, image_path):
                return []

        monkeypatch.setitem(
            sys.modules,
            "paddleocr",
            types.SimpleNamespace(PaddleOCR=FakePaddleOCR),
        )

        backend = PaddleBackend()
        threads = [threading.Thread(target=backend._ensure_loaded) for _ in range(4)]

        for thread in threads:
            thread.start()
        start_event.set()
        for thread in threads:
            thread.join()

        assert init_count == 1
        assert backend._ocr is not None


# ─────────────────────────────────────────────────────────────────────────────
# AppleVisionBackend 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestAppleVisionBackend:
    def test_available_on_macos_only(self):
        backend = AppleVisionBackend()
        if sys.platform == "darwin":
            assert backend.is_available() is True
        else:
            assert backend.is_available() is False

    def test_run_raises_on_non_macos(self):
        """非 macOS 环境调用 run() 应立即抛出 RuntimeError"""
        if sys.platform == "darwin":
            pytest.skip("macOS 上 AppleVisionBackend 是可用的")
        backend = AppleVisionBackend()
        with pytest.raises(RuntimeError, match="仅在 macOS"):
            backend.run("/tmp/dummy.jpg")

    def test_pyobjc_bbox_is_converted_from_bottom_left_to_top_left(self):
        observation = types.SimpleNamespace(
            boundingBox=lambda: ((0.2, 0.1), (0.5, 0.25))
        )

        bbox = PyObjCAppleVisionBackend._normalized_bbox(observation)

        assert [value for point in bbox for value in point] == pytest.approx(
            [0.2, 0.65, 0.7, 0.65, 0.7, 0.9, 0.2, 0.9]
        )


# ─────────────────────────────────────────────────────────────────────────────
# MockOcrBackend 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestMockOcrBackend:
    def test_default_returns_fixed_output(self):
        mock = MockOcrBackend()
        output = mock.run("/any/path.jpg")
        assert output.text == "模拟识别文字"
        assert output.confidence == pytest.approx(0.95)

    def test_custom_output(self):
        custom = OcrOutput(
            boxes=[OcrBox(text="自定义文字", confidence=0.75)],
            language="en",
        )
        mock = MockOcrBackend(output=custom)
        output = mock.run("/path.jpg")
        assert output.text == "自定义文字"
        assert output.language == "en"

    def test_raises_when_configured(self):
        mock = MockOcrBackend(should_raise=RuntimeError("模拟 OCR 失败"))
        with pytest.raises(RuntimeError, match="模拟 OCR 失败"):
            mock.run("/path.jpg")

    def test_unavailable(self):
        mock = MockOcrBackend(available=False)
        assert not mock.is_available()

    def test_call_count(self):
        mock = MockOcrBackend()
        mock.run("/p1.jpg")
        mock.run("/p2.jpg")
        assert mock.call_count == 2

    def test_is_backend_subclass(self):
        """确保 MockOcrBackend 实现了 OcrBackend 接口"""
        assert isinstance(MockOcrBackend(), OcrBackend)
