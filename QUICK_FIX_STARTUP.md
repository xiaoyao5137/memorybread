# 快速修复：记忆面包启动卡住问题

## 症状

应用启动后一直显示"仍在启动中，通常在 3 分钟内完成…"，无法进入主界面。

## 快速解决（5分钟）

### 方法一：一键修复脚本

1. 打开终端（Terminal.app）

2. 运行以下命令：

```bash
cd /Users/xianjiaqi/Documents/mygit/VibeWorking
./fix-startup-stuck.sh
```

3. 按照脚本提示操作即可

### 方法二：手动修复

#### 步骤 1：关闭应用

如果应用正在运行，请完全关闭（⌘Q 或从 Dock 右键退出）。

#### 步骤 2：启动 Ollama 服务

打开终端，复制粘贴并运行：

```bash
# 进入 Ollama 目录
cd "$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/initialization/runtime/ollama/v0.30.8/runtime"

# 设置环境变量
export OLLAMA_MODELS="$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/initialization/models"
export OLLAMA_HOST="127.0.0.1:11434"

# 启动服务
./ollama serve > ~/ollama-manual.log 2>&1 &
```

等待几秒，然后验证服务是否运行：

```bash
ps aux | grep "ollama serve"
```

如果看到进程信息，说明服务启动成功。

#### 步骤 3：重启应用

重新打开「记忆面包」应用。

- 如果看到"恢复初始化"按钮，点击它
- 如果看到"初始化"按钮，点击它
- 等待初始化完成（3-10分钟）

## 验证修复

成功的标志：

1. 能看到初始化进度条从 0% 开始增长
2. 各个阶段依次完成（打勾✓）
3. 最终进入主界面，看到"咨询"、"创作"等功能按钮

## 如果问题仍然存在

### 查看日志

```bash
# 查看最近的错误
tail -100 "$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/logs/sidecar.log"
```

### 完全重置初始化

**注意：这会清空初始化状态，但不会删除已下载的模型**

```bash
STATE_FILE="$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/initialization/state.json"

# 1. 备份当前状态
cp "$STATE_FILE" "${STATE_FILE}.backup.$(date +%Y%m%d_%H%M%S)"

# 2. 重置为初始状态
cat "$STATE_FILE" | \
  sed 's/"state": "[^"]*"/"state": "not_started"/' | \
  sed 's/"run_id": .*/"run_id": null,/' | \
  sed 's/"can_retry": [^,]*/"can_retry": false/' \
  > "${STATE_FILE}.tmp"

mv "${STATE_FILE}.tmp" "$STATE_FILE"

echo "✅ 初始化状态已重置，请重新打开应用"
```

### 检查应用版本

如果使用的是旧版本（8月18日之前的版本），建议：

1. 检查应用版本：打开应用 → 设置 → 关于
2. 如果版本 < 0.1.3，重新构建最新版本：

```bash
cd /Users/xianjiaqi/Documents/mygit/VibeWorking

# 重新构建
npm install -C desktop-ui
npm run build -C desktop-ui
cd desktop-ui
npm run tauri build

# 安装新版本
open src-tauri/target/release/bundle/macos/记忆面包.app
```

## 联系支持

如果以上方法都无法解决问题，请提供以下信息：

1. **系统信息**
   ```bash
   sw_vers
   ```

2. **应用版本**
   打开应用 → 设置 → 关于

3. **初始化状态**
   ```bash
   cat "$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/initialization/state.json"
   ```

4. **日志文件**
   ```bash
   tar -czf ~/memory-bread-logs.tar.gz \
     "$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/logs/"
   ```

将 `~/memory-bread-logs.tar.gz` 发送给技术支持。

## 常见问题

### Q: 为什么会卡在启动界面？

A: 通常是因为：
1. 之前的初始化被意外中断（如强制关闭应用）
2. Ollama 服务未自动启动
3. macOS 版本检测错误（已在最新版本修复）

### Q: 初始化需要多长时间？

A: 
- 首次初始化：5-15分钟（需要下载模型，约3GB）
- 恢复初始化：3-5分钟（模型已存在，只需验证）

### Q: 初始化会占用多少空间？

A: 约 6GB（包括 Ollama 运行时和模型）

### Q: 可以跳过初始化吗？

A: 不建议。初始化是确保所有核心功能（本地AI、语义搜索、知识库）正常工作的必要步骤。

### Q: 初始化失败会影响已有数据吗？

A: 不会。初始化只安装运行时组件和模型，不涉及用户数据。

## 技术说明

这个问题的根本原因是：

1. **状态不一致**：初始化状态保存为 `interrupted`，但前端没有正确显示
2. **服务依赖**：model_api 服务依赖 Ollama，Ollama 未运行导致服务链断裂
3. **版本检测bug**（已修复）：旧版本在 PyInstaller 环境下误报 macOS 版本

最新版本（提交 cd06e8b）已修复 macOS 版本检测问题。如果问题仍然存在，请确保：
- 使用最新版本
- Ollama 服务正常运行
- 初始化状态正确

## 相关文档

- 完整分析：STARTUP_STUCK_ANALYSIS.md
- 修复脚本：fix-startup-stuck.sh
- macOS版本检测修复：MONITOR_FIX_SOLUTION.md
