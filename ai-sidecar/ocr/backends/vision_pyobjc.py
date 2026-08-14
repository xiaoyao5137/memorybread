"""
Apple Vision OCR 后端（macOS only）- PyObjC 实现

通过 PyObjC 直接调用 macOS Vision Framework，无需 subprocess 和编译。
优点：性能好（30-100ms），无编译开销，代码简洁。
缺点：需要安装 pyobjc-framework-Vision（约 50MB）。

用途：macOS 上的主 OCR 引擎。
"""

from __future__ import annotations

import logging
import sys

from .base import OcrBackend, OcrBox, OcrOutput

logger = logging.getLogger(__name__)


class AppleVisionBackend(OcrBackend):
    """
    Apple Vision OCR 后端（仅 macOS 11+）。

    使用 PyObjC 直接调用 Vision Framework，无需 subprocess。
    """

    @staticmethod
    def _normalized_bbox(observation) -> list[list[float]]:
        """把 Vision 左下角原点的归一化矩形转为左上角原点四点框。"""
        try:
            rect = observation.boundingBox()
            try:
                x = float(rect.origin.x)
                y = float(rect.origin.y)
                width = float(rect.size.width)
                height = float(rect.size.height)
            except AttributeError:
                (x, y), (width, height) = rect
                x = float(x)
                y = float(y)
                width = float(width)
                height = float(height)
        except (AttributeError, TypeError, ValueError):
            return []
        if width <= 0 or height <= 0:
            return []
        left = max(0.0, min(1.0, x))
        right = max(left, min(1.0, x + width))
        top = max(0.0, min(1.0, 1.0 - y - height))
        bottom = max(top, min(1.0, 1.0 - y))
        return [
            [left, top],
            [right, top],
            [right, bottom],
            [left, bottom],
        ]

    def is_available(self) -> bool:
        """检查是否在 macOS 上且 PyObjC 可用"""
        if sys.platform != "darwin":
            return False

        try:
            import Vision  # noqa: F401
            import Quartz  # noqa: F401
            return True
        except ImportError:
            logger.debug("PyObjC Vision Framework 未安装")
            return False

    def run(self, image_path: str) -> OcrOutput:
        """执行 OCR 识别"""
        if not self.is_available():
            raise RuntimeError("Apple Vision OCR 仅在 macOS 上可用")

        try:
            import Vision
            import Quartz
            from Foundation import NSURL
        except ImportError as e:
            raise RuntimeError(
                "PyObjC 未安装，请运行: pip install pyobjc-framework-Vision pyobjc-framework-Quartz"
            ) from e

        # 加载图片
        image_url = NSURL.fileURLWithPath_(image_path)
        image_source = Quartz.CGImageSourceCreateWithURL(image_url, None)

        if not image_source:
            raise RuntimeError(f"无法加载图片: {image_path}")

        cg_image = Quartz.CGImageSourceCreateImageAtIndex(image_source, 0, None)

        if not cg_image:
            raise RuntimeError(f"无法解析图片: {image_path}")

        # 创建 Vision 请求
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        request.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US", "ja-JP"])

        # 创建请求处理器
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            cg_image, {}
        )

        # 执行识别
        success, error = handler.performRequests_error_([request], None)

        if not success or error:
            error_msg = str(error) if error else "未知错误"
            raise RuntimeError(f"Vision OCR 执行失败: {error_msg}")

        # 解析结果
        boxes: list[OcrBox] = []
        observations = request.results()

        if observations:
            for obs in observations:
                candidates = obs.topCandidates_(1)
                if candidates and len(candidates) > 0:
                    candidate = candidates[0]
                    text = candidate.string()
                    confidence = float(candidate.confidence())

                    if text and text.strip():
                        boxes.append(
                            OcrBox(
                                text=text,
                                confidence=confidence,
                                bbox=self._normalized_bbox(obs),
                            )
                        )

        logger.debug("Apple Vision 识别完成：%d 个文字框", len(boxes))
        return OcrOutput(boxes=boxes, language="zh")
