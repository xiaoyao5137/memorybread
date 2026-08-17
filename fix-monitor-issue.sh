#!/bin/bash
#
# 一键修复脚本：构建并安装修复版本
#
# 用法：
#   bash fix-monitor-issue.sh
#

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/tmp/monitor-fix-$(date +%Y%m%d-%H%M%S).log"

echo "========================================"
echo "  记忆面包 - 监控服务修复脚本"
echo "========================================"
echo ""
echo "本脚本将："
echo "  1. 重新构建应用（包含 macOS 版本检测修复）"
echo "  2. 安装新版本到 /Applications"
echo "  3. 重启应用"
echo "  4. 验证修复效果"
echo ""
echo "预计耗时: 20-30 分钟"
echo "日志文件: $LOG_FILE"
echo ""
read -p "按 Enter 继续，Ctrl+C 取消... "

# 1. 构建新版本
echo ""
echo "[1/4] 构建应用（包含修复）..."
echo "  → 日志: $LOG_FILE"
cd "$PROJECT_ROOT"
bash scripts/build-macos.sh dmg > "$LOG_FILE" 2>&1 &
BUILD_PID=$!

# 显示进度
echo "  构建中... (PID: $BUILD_PID)"
while kill -0 $BUILD_PID 2>/dev/null; do
    echo -n "."
    sleep 5
done
wait $BUILD_PID
BUILD_EXIT_CODE=$?

if [ $BUILD_EXIT_CODE -ne 0 ]; then
    echo ""
    echo "❌ 构建失败，退出码: $BUILD_EXIT_CODE"
    echo "请查看日志: $LOG_FILE"
    tail -50 "$LOG_FILE"
    exit 1
fi

echo ""
echo "✅ 构建完成"

# 2. 安装新版本
echo ""
echo "[2/4] 安装新版本..."
bash build-and-install-dmg.sh >> "$LOG_FILE" 2>&1
echo "✅ 安装完成"

# 3. 停止守护进程（如果在运行）
echo ""
echo "[3/4] 清理守护进程..."
pkill -f runtime_status_watchdog || true
echo "✅ 清理完成"

# 4. 重启应用
echo ""
echo "[4/4] 重启应用..."
pkill -f "memory-bread" || true
sleep 3
open /Applications/记忆面包.app
echo "✅ 应用已重启"

# 等待服务启动
echo ""
echo "等待服务启动..."
for i in {1..30}; do
    if curl -s http://localhost:7070/health > /dev/null 2>&1; then
        break
    fi
    echo -n "."
    sleep 1
done
echo ""

# 验证修复
echo ""
echo "========================================"
echo "  验证修复结果"
echo "========================================"
echo ""

echo "系统版本:"
sw_vers

echo ""
echo "Python 检测的版本:"
curl -s http://localhost:7071/api/ollama/setup-status 2>/dev/null | \
    python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)['detail']
    print(f\"  system_version: {d['system_version']}\")
    print(f\"  version_compatible: {d['version_compatible']}\")
    if d['version_compatible']:
        print('  ✅ 版本检测正常')
    else:
        print('  ❌ 版本检测仍有问题')
except:
    print('  ⚠️  无法获取版本信息')
" || echo "  ⚠️  服务尚未完全启动"

echo ""
echo "服务健康状态 (等待10秒后检查):"
sleep 10
curl -s "http://localhost:7070/api/monitor/overview?range_ms=21600000" 2>/dev/null | \
    python3 -c "
import sys, json
try:
    sh = json.load(sys.stdin)['service_health']
    print(f\"  状态: {sh['status']}\")
    print(f\"  模式: {sh['mode']}\")
    print(f\"  full_dispatch_ready: {sh['full_dispatch_ready']}\")
    print(f\"  critical_checks_passed: {sh['critical_checks_passed']}\")
    print(f\"  background_processor_running: {sh['background_processor_running']}\")
    print(f\"  问题: {sh['issues']}\")
    print()
    if sh['mode'] == 'full' and sh['full_dispatch_ready']:
        print('  ✅ 修复成功！所有服务正常运行')
    else:
        print('  ⚠️  服务仍未完全启动，请查看日志')
        print(f\"  日志: {LOG_FILE}\")
except Exception as e:
    print(f'  ❌ 无法获取服务状态: {e}')
" || echo "  ⚠️  服务尚未完全启动，请稍后再次检查"

echo ""
echo "========================================"
echo "  修复流程完成"
echo "========================================"
echo ""
echo "如果服务仍有问题，请："
echo "  1. 查看完整日志: cat $LOG_FILE"
echo "  2. 查看应用日志: open ~/Library/Logs"
echo "  3. 检查服务状态: curl http://localhost:7070/api/monitor/overview"
echo ""
