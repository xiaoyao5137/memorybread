#!/bin/bash

echo "=== 记忆面包启动问题快速诊断 ==="
echo ""

# 1. 检查应用是否安装
APP_PATH="/Applications/记忆面包.app"
if [ ! -d "$APP_PATH" ]; then
    echo "✗ 未找到应用，请先安装记忆面包.app"
    exit 1
fi
echo "✓ 应用已安装: $APP_PATH"
echo ""

# 2. 检查后端二进制文件
echo "检查后端服务文件..."
CORE_PATH="$APP_PATH/Contents/MacOS/memory-bread-core"
AI_PATH="$APP_PATH/Contents/Resources/binaries/memory-bread-ai.app/Contents/MacOS/memory-bread-ai"

if [ -f "$CORE_PATH" ]; then
    echo "  ✓ core-engine: $(ls -lh "$CORE_PATH" | awk '{print $5}')"
else
    echo "  ✗ core-engine 缺失"
fi

if [ -f "$AI_PATH" ]; then
    echo "  ✓ ai-sidecar: $(ls -lh "$AI_PATH" | awk '{print $5}')"
else
    echo "  ✗ ai-sidecar 缺失或路径错误"
    echo "    期望路径: $AI_PATH"
    exit 1
fi
echo ""

# 3. 测试 ai-sidecar 能否启动
echo "测试 ai-sidecar 模块加载..."
TEST_OUTPUT=$("$AI_PATH" --help 2>&1 | head -5)
if echo "$TEST_OUTPUT" | grep -q "Module object for struct is NULL"; then
    echo "  ✗ PyInstaller _struct 模块加载失败"
    echo "  原因: PyInstaller 6.21.0 在 macOS 上的已知 bug"
    echo "  解决: 需要使用 PyInstaller 6.22+ 重新打包"
    echo ""
    echo "  详细错误:"
    echo "$TEST_OUTPUT" | sed 's/^/    /'
    exit 1
elif echo "$TEST_OUTPUT" | grep -q "usage:\|command:"; then
    echo "  ✓ 模块加载正常"
else
    echo "  ? 无法判断状态，输出:"
    echo "$TEST_OUTPUT" | sed 's/^/    /'
fi
echo ""

# 4. 检查端口占用
echo "检查服务端口..."
if lsof -i :7070 -i :7071 | grep LISTEN > /dev/null 2>&1; then
    echo "  ⚠ 服务端口已被占用，可能有其他实例在运行"
    lsof -i :7070 -i :7071 | grep LISTEN | awk '{print "    " $1 " (PID " $2 ") - " $9}'
else
    echo "  ✓ 端口空闲"
fi
echo ""

# 5. 检查日志
LOG_DIR="$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/logs"
if [ -d "$LOG_DIR" ]; then
    echo "最近的日志文件:"
    ls -lth "$LOG_DIR"/*.log 2>/dev/null | head -5 | awk '{print "  " $9 " (" $5 ", " $6 " " $7 " " $8 ")"}'

    # 检查 sidecar 日志中的错误
    if [ -f "$LOG_DIR/sidecar.log" ]; then
        echo ""
        echo "sidecar 日志最后 10 行:"
        tail -10 "$LOG_DIR/sidecar.log" | sed 's/^/  /'
    fi
else
    echo "  ℹ 尚未生成日志文件（首次运行）"
fi
echo ""

echo "=== 诊断完成 ==="
echo ""
echo "如果发现 _struct 模块错误，请："
echo "1. 从源代码重新构建（使用 PyInstaller 6.22+）"
echo "2. 或等待官方发布修复版本"
echo ""
echo "如果其他错误，请查看完整日志："
echo "  $LOG_DIR/"
