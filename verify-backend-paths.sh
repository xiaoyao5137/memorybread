#!/bin/bash

APP_PATH="/Applications/记忆面包.app"

echo "=== 验证后端服务路径 ==="
echo ""

echo "1. 检查 memory-bread-core:"
CORE_PATH="$APP_PATH/Contents/MacOS/memory-bread-core"
if [ -f "$CORE_PATH" ]; then
    echo "   ✓ 找到: $CORE_PATH"
    ls -lh "$CORE_PATH"
else
    echo "   ✗ 未找到: $CORE_PATH"
fi
echo ""

echo "2. 检查 memory-bread-ai (错误路径 - Helpers):"
AI_WRONG_PATH="$APP_PATH/Contents/Helpers/memory-bread-ai.app/Contents/MacOS/memory-bread-ai"
if [ -f "$AI_WRONG_PATH" ]; then
    echo "   ✗ 在错误路径找到: $AI_WRONG_PATH"
else
    echo "   ✓ 错误路径不存在（符合预期）"
fi
echo ""

echo "3. 检查 memory-bread-ai (正确路径 - Resources/binaries):"
AI_CORRECT_PATH="$APP_PATH/Contents/Resources/binaries/memory-bread-ai.app/Contents/MacOS/memory-bread-ai"
if [ -f "$AI_CORRECT_PATH" ]; then
    echo "   ✓ 找到: $AI_CORRECT_PATH"
    ls -lh "$AI_CORRECT_PATH"
else
    echo "   ✗ 未找到: $AI_CORRECT_PATH"
fi
echo ""

echo "4. 检查运行时目录:"
RUNTIME_DIR="$HOME/Library/Application Support/com.memory-bread.app/runtime"
if [ -d "$RUNTIME_DIR" ]; then
    echo "   ✓ 运行时目录存在"
    echo "   内容:"
    ls -la "$RUNTIME_DIR/.memory-bread/" 2>/dev/null | head -15
else
    echo "   ✗ 运行时目录不存在"
fi
echo ""

echo "5. 检查后端进程:"
echo "   ai-sidecar (7071):"
if lsof -i :7071 | grep LISTEN > /dev/null 2>&1; then
    echo "   ✓ 端口 7071 正在监听"
else
    echo "   ✗ 端口 7071 未监听"
fi

echo "   core-engine (7070):"
if lsof -i :7070 | grep LISTEN > /dev/null 2>&1; then
    echo "   ✓ 端口 7070 正在监听"
else
    echo "   ✗ 端口 7070 未监听"
fi
echo ""

echo "6. 测试 API 连接:"
echo "   测试 sidecar 初始化状态:"
curl -s -m 2 http://127.0.0.1:7071/api/initialization/status 2>&1 | head -5
echo ""
