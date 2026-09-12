"""
OcrEngine — OCR 引擎编排器

职责：
- 选择可用的 OCR 后端（primary → fallback）
- 处理后端故障的降级逻辑
- 对图片路径进行预检（文件存在性）

后端优先级（默认配置）：
    macOS:   1. Apple Vision（原生，更快更省电）→ 2. PaddleOCR（降级）
    其他平台: 1. PaddleOCR（跨平台）→ 2. 无降级
"""

from __future__ import annotations

import logging
import os
import platform
from typing import Optional
from .control import OcrDeferred, current_control

from .backends.base   import OcrBackend, OcrOutput
from .backends.paddle import PaddleBackend
from .backends.vision_pyobjc import AppleVisionBackend

logger = logging.getLogger(__name__)


class OcrEngine:
    """
    OCR 引擎编排器。

    通过依赖注入接受后端实例，方便在测试中替换为 MockBackend。

    使用方式：
        engine = OcrEngine.create_default()
        output = engine.process("/path/to/screenshot.jpg")
    """

    def __init__(
        self,
        primary: Optional[OcrBackend] = None,
        fallback: Optional[OcrBackend] = None,
    ) -> None:
        self._primary  = primary
        self._fallback = fallback

    @classmethod
    def create_default(cls) -> "OcrEngine":
        """
        工厂方法：根据平台自动选择最优后端。

        macOS: Apple Vision (primary) + PaddleOCR (fallback)
        其他:  PaddleOCR (primary) + 无 fallback
        """
        system = platform.system()

        if system == "Darwin":  # macOS
            logger.info("检测到 macOS，使用 Apple Vision 作为主 OCR 引擎")
            return cls(
                primary=AppleVisionBackend(),
                fallback=PaddleBackend(lang="ch"),
            )
        else:  # Windows / Linux
            logger.info("检测到 %s，使用 PaddleOCR 作为主 OCR 引擎", system)
            return cls(
                primary=PaddleBackend(lang="ch"),
                fallback=None,
            )

    # ── 核心处理 ──────────────────────────────────────────────────────────────

    def process(self, image_path: str) -> OcrOutput:
        """
        对截图文件执行 OCR。

        Args:
            image_path: 截图文件的绝对路径（JPEG/PNG）

        Returns:
            OcrOutput 包含识别文本和置信度

        Raises:
            FileNotFoundError: 文件不存在
            RuntimeError:      所有后端均失败
        """
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"截图文件不存在: {image_path}")

        control = current_control()
        if control:
            control.check()

        # 尝试 primary 后端
        if self._primary and self._primary.is_available():
            try:
                output = self._primary.run(image_path)
                logger.debug(
                    "primary OCR 完成: %d 行, 置信度=%.3f",
                    len(output.boxes), output.confidence,
                )
                return output
            except OcrDeferred:
                raise
            except Exception as exc:
                logger.warning("primary OCR 失败，尝试 fallback: %s", exc)

        if control:
            control.check()
        # 尝试 fallback 后端
        if self._fallback and self._fallback.is_available():
            try:
                output = self._fallback.run(image_path)
                logger.debug(
                    "fallback OCR 完成: %d 行, 置信度=%.3f",
                    len(output.boxes), output.confidence,
                )
                return output
            except OcrDeferred:
                raise
            except Exception as exc:
                logger.error("fallback OCR 也失败: %s", exc)
                raise RuntimeError(f"所有 OCR 后端均失败: {exc}") from exc

        raise RuntimeError(
            "没有可用的 OCR 后端（PaddleOCR 未安装且不在 macOS 上）"
        )
