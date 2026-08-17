# 记忆面包启动卡住问题分析

## 问题描述

用户报告：软件启动后一直卡在"仍在启动中，通常在 3 分钟内完成…"步骤，无法进入主界面。

## 根本原因

通过诊断发现以下问题链：

### 1. 初始化状态异常

```json
{
  "state": "interrupted",
  "error_code": "INITIALIZATION_COMPONENT_MISSING",
  "message": "检测到本地能力需要恢复"
}
```

初始化流程在之前运行时被中断，状态保存为 `interrupted`，需要用户手动点击"恢复初始化"。

### 2. Ollama 服务未运行

日志显示：
```
❌ Ollama 服务未运行
📝 启动方法：ollama serve
```

初始化需要 Ollama 服务运行才能继续，但服务未启动。

### 3. macOS 版本检测误报（已修复）

早期版本的代码在 PyInstaller 打包环境下会错误地报告 macOS 版本为 10.16：

```
核心启动检查未通过，保持基础 IPC 模式
原因: 当前 macOS 10.16，建议升级到 12+ 后再安装 Ollama
```

实际系统版本是 macOS 12.7.6（Monterey），完全符合要求。

这个问题在最新提交 (cd06e8b) 中已经修复，但用户运行的可能是旧版本。

### 4. 前端状态显示不准确

前端代码在连接后端时，如果连接失败会进入重试循环，显示"仍在启动中"：

```typescript
// desktop-ui/src/components/OnboardingWizard.tsx:166
} else {
  setConnecting(true)  // 显示"仍在启动中"
}
timer = setTimeout(refresh, SIDECAR_RETRY_MS)
```

虽然后端实际返回了 `interrupted` 状态，但由于连接问题，前端没有收到状态，一直显示连接中。

## 技术细节

### 后端服务状态

运行的进程：
```
memory-bread-desktop (主应用)
memory-bread-ai sidecar (IPC 服务，基础模式)
```

**缺失的进程：**
- `model_api` (Flask 服务，7071端口) - 因为预检查失败未启动
- `ollama serve` (AI 引擎) - 未运行

### 日志分析

`sidecar.log` 显示：
```
核心启动检查未通过，保持基础 IPC 模式，仅保留 ping/OCR 能力
```

这意味着 sidecar 启动了，但因为预检查失败（Ollama 未运行或版本检测失败），只提供基础功能，不启动完整的 model_api 服务。

### 初始化文件状态

已安装组件：
- ✅ Ollama 运行时：`$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/initialization/runtime/ollama/v0.30.8/`
- ✅ 模型文件：qwen3.5:4b 和 bge-small-zh-v1.5（blobs已下载）
- ✅ 数据库：memory-bread.db

但初始化状态显示为 `interrupted`，说明质检未通过。

## 解决方案

### 方案 1：使用修复脚本（推荐）

运行提供的诊断修复脚本：

```bash
cd /Users/xianjiaqi/Documents/mygit/VibeWorking
./fix-startup-stuck.sh
```

脚本会：
1. 检查并重置初始化状态
2. 启动 Ollama 服务
3. 清理残留进程
4. 给出明确的下一步指引

### 方案 2：手动修复

#### 2.1 关闭应用

```bash
pkill -f "memory-bread-desktop"
pkill -f "memory-bread-ai"
```

#### 2.2 启动 Ollama 服务

```bash
cd "$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/initialization/runtime/ollama/v0.30.8/runtime"

export OLLAMA_MODELS="$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/initialization/models"
export OLLAMA_HOST="127.0.0.1:11434"

./ollama serve &
```

#### 2.3 重置初始化状态（可选）

如果想完全重新初始化：

```bash
STATE_FILE="$HOME/Library/Application Support/com.memory-bread.app/runtime/.memory-bread/initialization/state.json"

# 备份
cp "$STATE_FILE" "$STATE_FILE.backup"

# 重置
cat "$STATE_FILE" | \
  sed 's/"state": "interrupted"/"state": "not_started"/' | \
  sed 's/"run_id": .*/"run_id": null,/' \
  > "$STATE_FILE.tmp"
mv "$STATE_FILE.tmp" "$STATE_FILE"
```

#### 2.4 重启应用

打开「记忆面包」应用，点击"初始化"或"恢复初始化"按钮。

### 方案 3：重新安装最新版本

如果使用的是旧版本（有 macOS 版本检测bug），建议：

1. 完全卸载旧版本
2. 下载最新版本（包含cd06e8b修复）
3. 重新安装

## 预防措施

### 1. 改进前端错误处理

当连接重试超过一定次数（如60秒）后，应该：
- 停止显示"启动中"
- 显示明确的错误信息
- 提供"手动排查"链接

已在 `OnboardingWizard.tsx:166` 添加：

```typescript
if (attempts > 20) {  // 1分钟后
  setConnecting(false)
  setConnectionError('本地初始化服务连接超时，请检查应用是否正常运行')
}
```

### 2. 后端健康检查改进

在应用启动时，如果检测到：
- 初始化状态为 `interrupted`
- Ollama 已安装但未运行

应该自动尝试启动 Ollama 服务，而不是等待用户手动操作。

### 3. 初始化恢复机制

当检测到 `interrupted` 状态时，应该：
1. 检查哪些组件已经安装成功
2. 只重新执行失败的阶段
3. 避免重复下载已有的模型

当前代码已经支持跳过已完成的阶段，但在 `interrupted` 状态下可能没有正确触发。

## 相关文件

- 前端：`desktop-ui/src/components/OnboardingWizard.tsx`
- 前端工具：`desktop-ui/src/utils/initialization.ts`
- 后端管理器：`ai-sidecar/initialization_manager.py`
- 模型管理：`ai-sidecar/model_manager.py`
- 启动检查：`ai-sidecar/startup_checks.py`

## 测试验证

修复后需要验证：

1. **正常启动流程**
   - 全新安装 → 初始化 → 成功进入主界面

2. **中断恢复流程**
   - 初始化中断 → 重启应用 → 显示"恢复初始化"按钮 → 点击恢复 → 成功

3. **连接超时处理**
   - 后端服务未启动 → 前端60秒后显示明确错误 → 不再显示"启动中"

4. **macOS版本检测**
   - 在 PyInstaller 打包环境下正确识别 macOS 12.x
   - 不误报为 10.16

## 后续优化建议

1. **一键诊断功能**
   - 在应用中内置"诊断启动问题"功能
   - 自动检测和修复常见问题
   - 生成诊断报告供技术支持使用

2. **初始化进度持久化**
   - 保存每个阶段的详细状态
   - 中断后能精确恢复到上次位置
   - 避免重复下载

3. **离线安装包**
   - 提供包含所有模型的完整安装包
   - 减少网络问题导致的初始化失败

4. **更好的错误提示**
   - 错误信息更加用户友好
   - 提供具体的解决步骤
   - 添加"复制诊断信息"按钮

## 参考

- 相关问题：#issue-macos-version-detection
- 修复提交：cd06e8b (fix: 修复 PyInstaller 环境下 macOS 版本检测问题)
- 修复提交：f094f03 (fix: 修复 macOS 应用启动卡住问题)
- 文档：MONITOR_FIX_SOLUTION.md
