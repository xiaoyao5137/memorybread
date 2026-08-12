#!/usr/bin/env python3
"""
测试 OCR 集成 - 直接通过 IPC 调用 AI Sidecar

这个脚本模拟 Core Engine 的行为，通过 Unix Socket 发送 OCR 请求
"""

import asyncio
import json
import os
import struct
import sys
import time
import uuid
from pathlib import Path

# 添加 ai-sidecar 到 Python 路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-sidecar"))

from memory_bread_ipc import IpcRequest, IpcResponse, OcrRequest

SOCKET_PATH = "/tmp/memory-bread-sidecar.sock"
SCREENSHOT_DIR = os.path.expanduser("~/.memory-bread/captures/screenshots")


async def send_frame(writer: asyncio.StreamWriter, data: dict):
    """发送帧：4字节长度 + JSON"""
    json_bytes = json.dumps(data).encode('utf-8')
    length = len(json_bytes)
    frame = struct.pack('>I', length) + json_bytes
    writer.write(frame)
    await writer.drain()


async def receive_frame(reader: asyncio.StreamReader) -> dict:
    """接收帧：4字节长度 + JSON"""
    length_bytes = await reader.readexactly(4)
    length = struct.unpack('>I', length_bytes)[0]
    json_bytes = await reader.readexactly(length)
    return json.loads(json_bytes.decode('utf-8'))


async def test_ocr_via_ipc():
    """通过 IPC 测试 OCR"""

    # 1. 找到最新的截图
    screenshots = sorted([
        f for f in os.listdir(SCREENSHOT_DIR)
        if f.endswith('.jpg')
    ])

    if not screenshots:
        print("❌ 没有找到截图文件")
        return

    latest_screenshot = os.path.join(SCREENSHOT_DIR, screenshots[-1])
    print(f"📸 使用截图: {latest_screenshot}")
    print(f"   文件大小: {os.path.getsize(latest_screenshot) / 1024:.1f} KB")

    # 2. 创建 IPC 请求
    request = IpcRequest(
        id=str(uuid.uuid4()),
        ts=int(time.time() * 1000),
        task=OcrRequest(
            capture_id=1,
            screenshot_path=latest_screenshot
        )
    )

    print(f"\n📤 发送 OCR 请求...")
    print(f"   请求 ID: {request.id}")
    print(f"   截图路径: {request.task.screenshot_path}")

    # 3. 连接到 AI Sidecar
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
        print(f"✅ 已连接到 AI Sidecar: {SOCKET_PATH}")

        # 4. 发送请求
        t0 = time.time()
        await send_frame(writer, request.model_dump())

        # 5. 接收响应
        response_data = await receive_frame(reader)
        response = IpcResponse(**response_data)
        latency = int((time.time() - t0) * 1000)

        print(f"\n📥 收到响应 (耗时 {latency}ms)")
        print(f"   状态: {response.status}")
        print(f"   响应 ID: {response.id}")

        if response.status == "ok":
            result = response.result
            print(f"\n✅ OCR 成功!")

            # result 可能是 dict 或对象
            if isinstance(result, dict):
                text = result.get('text', '')
                confidence = result.get('confidence', 0.0)
                language = result.get('language', 'unknown')
            else:
                text = result.text
                confidence = result.confidence
                language = result.language

            print(f"   识别文字长度: {len(text)} 字符")
            print(f"   置信度: {confidence:.4f}")
            print(f"   语言: {language}")
            print(f"\n📝 识别文字（前 500 字符）:")
            print("=" * 60)
            print(text[:500])
            print("=" * 60)
        else:
            print(f"\n❌ OCR 失败")
            print(f"   错误: {response.error}")

        writer.close()
        await writer.wait_closed()

    except FileNotFoundError:
        print(f"❌ 无法连接到 AI Sidecar")
        print(f"   Socket 文件不存在: {SOCKET_PATH}")
        print(f"   请确保 AI Sidecar 正在运行")
    except Exception as e:
        print(f"❌ 发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("=" * 60)
    print("记忆面包 OCR 集成测试")
    print("=" * 60)
    asyncio.run(test_ocr_via_ipc())
