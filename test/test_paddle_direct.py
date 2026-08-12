#!/usr/bin/env python3
"""简单的 PaddleOCR 测试 - 不通过 IPC"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-sidecar"))

from ocr.backends.paddle import PaddleBackend

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

# 创建 PaddleOCR 后端
print("\n正在初始化 PaddleOCR...")
backend = PaddleBackend(lang="ch")

if not backend.is_available():
    print("❌ PaddleOCR 不可用")
    sys.exit(1)

print("✅ PaddleOCR 可用")

# 执行 OCR
print("\n正在执行 OCR（可能需要 10-30 秒）...")
import time
t0 = time.time()

try:
    result = backend.run(latest_screenshot)
    latency = time.time() - t0

    print(f"\n✅ OCR 完成！耗时 {latency:.1f} 秒")
    print(f"   识别文字长度: {len(result.text)} 字符")
    print(f"   置信度: {result.confidence:.4f}")
    print(f"   语言: {result.language}")
    print(f"\n📝 识别文字（前 500 字符）:")
    print("=" * 60)
    print(result.text[:500])
    print("=" * 60)

except Exception as e:
    print(f"\n❌ OCR 失败: {e}")
    import traceback
    traceback.print_exc()
