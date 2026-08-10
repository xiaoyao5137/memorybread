"""
ai-sidecar OCR 模块

对外暴露：
- OcrEngine : 引擎编排器（带 primary/fallback 后端）
- OcrWorker : IPC 任务处理器
- OcrBackend, OcrBox, OcrOutput : 后端接口与数据类型
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .engine          import OcrEngine
from .backends.base   import OcrBackend, OcrBox, OcrOutput
from .backends.paddle import PaddleBackend
from .backends.vision import AppleVisionBackend

if TYPE_CHECKING:
    from .worker import OcrWorker


def __getattr__(name: str) -> Any:
    """按需加载 IPC Worker，避免纯 OCR 调用被 IPC 运行时依赖阻断。"""
    if name == "OcrWorker":
        from .worker import OcrWorker

        return OcrWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "OcrEngine",
    "OcrWorker",
    "OcrBackend",
    "OcrBox",
    "OcrOutput",
    "PaddleBackend",
    "AppleVisionBackend",
]
