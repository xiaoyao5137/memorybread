#!/bin/bash
# 修复"记忆面包"应用启动卡住的问题
#
# 问题表现：应用启动后一直显示"仍在启动中，通常在 3 分钟内完成…"
#
# 根本原因：
# 1. 初始化状态为 interrupted（中断）
# 2. Ollama 服务没有运行
# 3. 前端没有正确显示错误状态，而是一直显示"启动中"

set -e

echo "==================================="
echo "记忆面包启动问题诊断与修复工具"
echo "==================================="
echo ""

# 1. 检查应用是否在运行
echo "1. 检查应用运行状态..."
if pgrep -f "memory-bread-desktop" > /dev/null; then
    echo "   ⚠️  应用正在运行，请先关闭应用"
    echo ""
    echo "   按任意键退出当前应用并继续..."
    read -n 1 -s
    pkill -f "memory-bread-desktop" || true
    sleep 2
fi
echo "   ✅ 应用未运行"
echo ""

# 2. 检查初始化状态
RUNTIME_DIR="$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread"
STATE_FILE="$RUNTIME_DIR/initialization/state.json"

echo "2. 检查初始化状态..."
if [ -f "$STATE_FILE" ]; then
    STATE=$(cat "$STATE_FILE" | grep '"state"' | head -1 | sed 's/.*: "\([^"]*\)".*/\1/')
    echo "   当前状态: $STATE"

    if [ "$STATE" = "interrupted" ] || [ "$STATE" = "failed" ]; then
        echo "   ⚠️  初始化处于异常状态，需要重置"
        echo ""
        echo "   是否重置初始化状态？(y/n)"
        read -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            # 备份当前状态
            cp "$STATE_FILE" "$STATE_FILE.backup.$(date +%Y%m%d_%H%M%S)"
            # 重置为 not_started 状态
            cat "$STATE_FILE" | \
                sed 's/"state": "interrupted"/"state": "not_started"/' | \
                sed 's/"state": "failed"/"state": "not_started"/' | \
                sed 's/"run_id": null/"run_id": null/' | \
                sed 's/"can_retry": true/"can_retry": false/' \
                > "$STATE_FILE.tmp"
            mv "$STATE_FILE.tmp" "$STATE_FILE"
            echo "   ✅ 初始化状态已重置"
        fi
    else
        echo "   ℹ️  状态正常 ($STATE)"
    fi
else
    echo "   ℹ️  状态文件不存在（首次运行）"
fi
echo ""

# 3. 检查 Ollama 安装
echo "3. 检查 Ollama 安装..."
OLLAMA_PATH="$RUNTIME_DIR/initialization/runtime/ollama/v0.30.8/runtime/ollama"
if [ -f "$OLLAMA_PATH" ]; then
    echo "   ✅ Ollama 已安装"
    echo "   路径: $OLLAMA_PATH"
else
    echo "   ❌ Ollama 未找到"
    echo "   应用首次启动时会自动下载安装"
fi
echo ""

# 4. 检查 Ollama 服务
echo "4. 检查 Ollama 服务..."
if pgrep -f "ollama serve" > /dev/null; then
    echo "   ✅ Ollama 服务正在运行"
else
    echo "   ⚠️  Ollama 服务未运行"

    if [ -f "$OLLAMA_PATH" ]; then
        echo ""
        echo "   是否启动 Ollama 服务？(y/n)"
        read -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "   正在启动 Ollama..."
            # 设置 OLLAMA_MODELS 环境变量
            export OLLAMA_MODELS="$RUNTIME_DIR/initialization/models"
            export OLLAMA_HOST="127.0.0.1:11434"

            # 后台启动 Ollama
            nohup "$OLLAMA_PATH" serve > "$RUNTIME_DIR/logs/ollama-manual.log" 2>&1 &
            sleep 3

            if pgrep -f "ollama serve" > /dev/null; then
                echo "   ✅ Ollama 服务启动成功"
            else
                echo "   ❌ Ollama 服务启动失败，请查看日志："
                echo "      $RUNTIME_DIR/logs/ollama-manual.log"
            fi
        fi
    fi
fi
echo ""

# 5. 检查模型
echo "5. 检查已安装的模型..."
if [ -f "$OLLAMA_PATH" ]; then
    export OLLAMA_MODELS="$RUNTIME_DIR/initialization/models"
    MODELS=$("$OLLAMA_PATH" list 2>/dev/null | tail -n +2 | awk '{print $1}' || echo "")
    if [ -n "$MODELS" ]; then
        echo "   已安装的模型："
        echo "$MODELS" | while read -r model; do
            echo "   - $model"
        done
    else
        echo "   ⚠️  未安装任何模型"
        echo "   应用首次初始化时会自动下载所需模型"
    fi
else
    echo "   ℹ️  Ollama 未安装，跳过模型检查"
fi
echo ""

# 6. 清理旧的后台服务进程
echo "6. 清理可能残留的后台服务..."
pkill -f "memory-bread-ai" || true
pkill -f "memory-bread-core" || true
sleep 1
echo "   ✅ 清理完成"
echo ""

# 7. 总结和下一步
echo "==================================="
echo "诊断完成！"
echo "==================================="
echo ""
echo "下一步操作："
echo "1. 启动「记忆面包」应用"
echo "2. 如果看到初始化界面，点击「初始化」或「恢复初始化」按钮"
echo "3. 等待初始化完成（通常 3-10 分钟）"
echo ""
echo "如果问题仍然存在："
echo "- 查看日志: $RUNTIME_DIR/logs/"
echo "- 联系技术支持并提供诊断信息"
echo ""
echo "按任意键退出..."
read -n 1 -s
