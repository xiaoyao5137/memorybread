"""
pytest 全局配置与 Fixture

主要职责：
1. 提供 MockOcrBackend：无需真实 PaddleOCR，返回可配置的 mock 结果
2. 提供 make_jpeg_image：程序化生成最小测试用 JPEG 文件（不依赖外部图片）
3. 提供 make_ipc_request：构造标准 IpcRequest 对象
"""

from __future__ import annotations

import io
import os
import tempfile
import uuid
from typing import Optional

import pytest

from ocr.backends.base import OcrBackend, OcrBox, OcrOutput


# ─────────────────────────────────────────────────────────────────────────────
# Mock 后端（核心测试工具）
# ─────────────────────────────────────────────────────────────────────────────

class MockOcrBackend(OcrBackend):
    """
    测试用 Mock OCR 后端。

    不调用任何真实 OCR 库，返回预设的 OcrOutput 或模拟指定异常。
    """

    def __init__(
        self,
        output:       Optional[OcrOutput] = None,
        should_raise: Optional[Exception] = None,
        available:    bool              = True,
    ) -> None:
        self._output       = output or OcrOutput(
            boxes=[OcrBox(text="模拟识别文字", confidence=0.95)],
            language="zh",
        )
        self._should_raise = should_raise
        self._available    = available
        self.call_count    = 0          # 测试可以断言调用次数

    def is_available(self) -> bool:
        return self._available

    def run(self, image_path: str) -> OcrOutput:
        self.call_count += 1
        if self._should_raise:
            raise self._should_raise
        return self._output


# ─────────────────────────────────────────────────────────────────────────────
# Fixture：临时 JPEG 图片
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def jpeg_image_path(tmp_path) -> str:
    """
    生成一个 4×4 像素的最小合法 JPEG 文件，返回其绝对路径。
    使用 Pillow 创建，不依赖外部图片文件。
    """
    from PIL import Image
    img = Image.new("RGB", (4, 4), color=(200, 100, 50))
    path = tmp_path / "test_shot.jpg"
    img.save(str(path), format="JPEG", quality=80)
    return str(path)


@pytest.fixture
def nonexistent_path(tmp_path) -> str:
    """返回一个确保不存在的文件路径"""
    return str(tmp_path / "ghost.jpg")


# ─────────────────────────────────────────────────────────────────────────────
# Fixture：IPC 消息构造助手
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def make_ocr_request():
    """工厂 Fixture：创建 OcrRequest 包装的 IpcRequest"""
    from memory_bread_ipc import IpcRequest, OcrRequest
    import time

    def _factory(screenshot_path: str = "/tmp/test.jpg", capture_id: int = 1):
        task = OcrRequest(capture_id=capture_id, screenshot_path=screenshot_path)
        return IpcRequest(id=str(uuid.uuid4()), ts=int(time.time() * 1000), task=task)

    return _factory


@pytest.fixture
def make_ping_request():
    """工厂 Fixture：创建 PingRequest 包装的 IpcRequest"""
    # PingRequest 在 memory_bread_ipc.message 中定义，但未在 __init__.py 中导出
    from memory_bread_ipc.message import IpcRequest, PingRequest
    import time

    def _factory():
        return IpcRequest(
            id=str(uuid.uuid4()),
            ts=int(time.time() * 1000),
            task=PingRequest(),
        )

    return _factory
