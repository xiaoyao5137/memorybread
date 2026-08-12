#!/usr/bin/env python3
"""
独立测试 Apple Vision OCR
不依赖 ai-sidecar 的其他模块
"""

import json
import subprocess
import sys
import glob
import os

# Swift OCR 脚本
SWIFT_OCR_SCRIPT = r"""
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
            results.append(["text": candidate.string, "confidence": Double(candidate.confidence)])
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

def test_apple_vision_ocr(image_path):
    """使用 Apple Vision 执行 OCR"""
    print(f"测试截图: {image_path}")
    print("🔄 开始 OCR 识别（首次运行需要编译 Swift，可能需要 30-60 秒）...\n")

    try:
        result = subprocess.run(
            ["swift", "-", image_path],
            input=SWIFT_OCR_SCRIPT,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        print("❌ OCR 超时（>60秒）")
        return False
    except FileNotFoundError:
        print("❌ swift 命令未找到")
        print("   安装方法: xcode-select --install")
        return False

    if result.returncode != 0:
        print(f"❌ swift 脚本执行失败（code={result.returncode}）")
        print(f"   错误: {result.stderr}")
        return False

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"❌ 无效的 JSON 输出: {result.stdout[:200]}")
        return False

    if "error" in data:
        print(f"❌ Apple Vision 错误: {data['error']}")
        return False

    results = data.get("results", [])

    if not results:
        print("⚠️  没有识别到文字（可能是空白截图）")
        return True

    # 提取文本
    texts = [item["text"] for item in results]
    full_text = "\n".join(texts)

    # 计算平均置信度
    confidences = [item.get("confidence", 0.0) for item in results]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    print(f"✅ OCR 识别成功!")
    print(f"文本框数: {len(results)}")
    print(f"平均置信度: {avg_confidence:.2f}")
    print(f"\n识别文本（前 800 字符）:\n{full_text[:800]}")

    if len(full_text) > 800:
        print(f"\n... (共 {len(full_text)} 字符)")

    return True

if __name__ == "__main__":
    # 获取最新截图
    screenshots = sorted(glob.glob(os.path.expanduser('~/.memory-bread/captures/screenshots/*.jpg')))

    if not screenshots:
        print("❌ 没有找到截图")
        print("   路径: ~/.memory-bread/captures/screenshots/")
        sys.exit(1)

    latest = screenshots[-1]

    success = test_apple_vision_ocr(latest)
    sys.exit(0 if success else 1)
