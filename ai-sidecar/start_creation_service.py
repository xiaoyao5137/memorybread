#!/usr/bin/env python3

from __future__ import annotations

"""启动创作服务"""
import os
import sys
import traceback
import uvicorn

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent))

from creation.app import app

if __name__ == "__main__":
    print("🚀 启动创作服务...")
    print("📍 监听地址: http://localhost:8001")
    print("📝 端点: POST /creation/generate")
    exit_code = 0
    try:
        uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    except BaseException:
        exit_code = 1
        traceback.print_exc()
    finally:
        # creation.app 导入时会初始化后台能力；若 Uvicorn 绑定失败或停止，
        # 非守护线程不能继续把这个专用服务进程伪装成“仍在运行”。
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(exit_code)
