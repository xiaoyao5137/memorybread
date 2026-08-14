"""
Apple Vision OCR 后端（macOS only）

通过 subprocess 调用内联 Swift 脚本，使用 macOS 原生 VNRecognizeTextRequest 识别文字。
优点：无需安装任何 Python 包，macOS 11+ 开箱即用。
缺点：需要 macOS，且每次识别需要启动 swift 进程（~200ms 额外开销）。

用途：PaddleOCR 不可用时的降级后端。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

from .base import OcrBackend, OcrBox, OcrOutput

logger = logging.getLogger(__name__)

# 内联 Swift 脚本：通过 Apple Vision VNRecognizeTextRequest 识别文字
# 接收一个参数：图片绝对路径
# 输出格式：{"results": [{"text": "...", "confidence": 0.95, "bbox": [...]}, ...]}
_SWIFT_OCR_SCRIPT = r"""
import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count > 1 else {
    print(#"{"error": "缺少图片路径参数"}"#)
    exit(1)
}

let imagePath = CommandLine.arguments[1]
let imageURL  = URL(fileURLWithPath: imagePath)

guard let nsImage = NSImage(contentsOf: imageURL),
      let cgImage = nsImage.cgImage(forProposedRect: nil, context: nil, hints: nil)
else {
    print(#"{"error": "无法加载图片"}"#)
    exit(1)
}

var results: [[String: Any]] = []

let semaphore = DispatchSemaphore(value: 0)
let request   = VNRecognizeTextRequest { req, err in
    defer { semaphore.signal() }
    guard err == nil, let obs = req.results as? [VNRecognizedTextObservation] else { return }
    for ob in obs {
        if let candidate = ob.topCandidates(1).first, !candidate.string.isEmpty {
            let rect = ob.boundingBox
            let left = Double(rect.origin.x)
            let right = Double(rect.origin.x + rect.size.width)
            let top = Double(1.0 - rect.origin.y - rect.size.height)
            let bottom = Double(1.0 - rect.origin.y)
            results.append([
                "text": candidate.string,
                "confidence": Double(candidate.confidence),
                "bbox": [[left, top], [right, top], [right, bottom], [left, bottom]],
            ])
        }
    }
}
request.recognitionLevel    = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages   = ["zh-Hans", "zh-Hant", "en-US", "ja-JP"]

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
try? handler.perform([request])
semaphore.wait()

let output = try! JSONSerialization.data(withJSONObject: ["results": results])
print(String(data: output, encoding: .utf8)!)
"""


class AppleVisionBackend(OcrBackend):
    """
    Apple Vision OCR 后端（仅 macOS 11+）。

    不依赖任何 Python 包，通过 subprocess 调用内联 Swift 脚本完成识别。
    """

    _SWIFT_TIMEOUT_SECS = 60  # swift 进程执行超时（秒）- 首次编译需要较长时间

    def is_available(self) -> bool:
        return sys.platform == "darwin"

    def run(self, image_path: str) -> OcrOutput:
        if not self.is_available():
            raise RuntimeError("Apple Vision OCR 仅在 macOS 上可用")

        try:
            result = subprocess.run(
                ["swift", "-", image_path],
                input=_SWIFT_OCR_SCRIPT,
                capture_output=True,
                text=True,
                timeout=self._SWIFT_TIMEOUT_SECS,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Apple Vision OCR 超时（>{self._SWIFT_TIMEOUT_SECS}s）"
            )
        except FileNotFoundError:
            raise RuntimeError(
                "swift 命令未找到，请安装 Xcode 命令行工具：xcode-select --install"
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"swift 脚本执行失败（code={result.returncode}）: {stderr}")

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Apple Vision 返回了无效 JSON: {result.stdout!r}"
            ) from e

        if "error" in data:
            raise RuntimeError(f"Apple Vision 错误: {data['error']}")

        boxes = [
            OcrBox(
                text=item["text"],
                confidence=float(item.get("confidence", 0.0)),
                bbox=item.get("bbox", []),
            )
            for item in data.get("results", [])
            if item.get("text", "").strip()
        ]

        logger.debug("Apple Vision 识别完成：%d 个文字框", len(boxes))
        return OcrOutput(boxes=boxes, language="zh")
