#!/usr/bin/env python3
"""测试 PyObjC Apple Vision OCR"""

import sys
import os
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-sidecar"))

from ocr.backends.vision_pyobjc import AppleVisionBackend

SCREENSHOT_DIR = os.path.expanduser("~/.memory-bread/captures/screenshots")

# 找到最新的截图
screenshots = sorted([
    f for f in os.listdir(SCREENSHOT_DIR)
    if f.endswith('.jpg')
])

if not screenshots:
    print("❌ 没有找到截图文件")
    sys.exit(1)

latest_screenshot = os.path.join(SCREENSHOT_DIR, screenshots[-1])
print(f"📸 测试截图: {latest_screenshot}")
print(f"   文件大小: {os.path.getsize(latest_screenshot) / 1024:.1f} KB")

# 创建 Apple Vision 后端
print("\n正在初始化 Apple Vision (PyObjC)...")
backend = AppleVisionBackend()

if not backend.is_available():
    print("❌ Apple Vision 不可用")
    sys.exit(1)

print("✅ Apple Vision 可用")

# 执行 OCR
print("\n正在执行 OCR...")
t0 = time.time()

try:
    result = backend.run(latest_screenshot)
    latency = int((time.time() - t0) * 1000)

    print(f"\n✅ OCR 完成！耗时 {latency} ms")
    print(f"   识别文字长度: {len(result.text)} 字符")
    print(f"   置信度: {result.confidence:.4f}")
    print(f"   语言: {result.language}")
    print(f"   文字框数量: {len(result.boxes)}")
    print(f"\n📝 识别文字（前 500 字符）:")
    print("=" * 60)
    print(result.text[:500])
    print("=" * 60)

except Exception as e:
    print(f"\n❌ OCR 失败: {e}")
    import traceback
    traceback.print_exc()
