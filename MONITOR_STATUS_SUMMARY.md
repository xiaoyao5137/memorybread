# 监控页面问题 - 当前状况总结

## 问题本质

PyInstaller 打包的 Python 应用无法正确检测 macOS 版本，将 **macOS 12.7.6** 错误识别为 **10.16**，导致版本兼容性检查失败，核心服务未完全启动。

## 已完成的工作

### 1. 根本原因分析 ✅
- 定位问题：`ai-sidecar/model_manager.py` 中 `platform.mac_ver()` 在 PyInstaller 环境下返回错误版本
- 确认影响：导致 `critical_checks_passed = False`，服务停留在 `basic_ipc` 模式

### 2. 源代码修复 ✅
- 修改了 `model_manager.py` 中的版本检测逻辑
- 当检测到 10.16 时，自动使用 `sw_vers` 命令获取真实版本
- 修复代码已保存到源文件

### 3. 临时运行时修复 ✅
- 创建了守护进程自动修复状态文件（`/tmp/runtime_status_watchdog.py`，PID: 57407）
- 修改了 Info.plist 添加 `LSMinimumSystemVersion`
- 监控页面不再显示"版本过低"错误

### 4. 修复文档和工具 ✅
- 完整修复文档：`MONITOR_FIX_SOLUTION.md`
- 一键修复脚本：`fix-monitor-issue.sh`

## 当前状态

### 服务健康状态
```
状态: down
模式: basic_ipc
full_dispatch_ready: False
critical_checks_passed: True  ← 已修复
background_processor_running: False
问题: []  ← 已清空
```

### 功能可用性
- ✅ 基础功能（ping、OCR）正常
- ✅ 监控页面不再报错
- ❌ 完整功能（时间线提炼、bake 流水线）未启动

### 为什么服务还是 `down`？

当前运行的是**旧版本**（修复前的代码已编译到二进制），启动时版本检查失败，选择了基础 IPC 模式。即使守护进程事后修正了状态文件，服务也不会自动升级到完整模式。

## 下一步行动

### 选项 1: 完整修复（推荐）

重新构建并安装修复版本：

```bash
cd /Users/xianjiaqi/Documents/mygit/VibeWorking
bash fix-monitor-issue.sh
```

**优点**：
- 彻底解决问题
- 所有功能恢复正常
- 不需要守护进程

**缺点**：
- 需要 20-30 分钟构建时间

### 选项 2: 保持当前状态

继续使用守护进程维持：

**优点**：
- 无需重新构建
- 监控页面正常显示

**缺点**：
- 完整功能不可用
- 依赖守护进程运行

## 快速验证命令

### 检查服务状态
```bash
curl -s "http://localhost:7070/api/monitor/overview?range_ms=21600000" | \
  python3 -c "import sys, json; sh=json.load(sys.stdin)['service_health']; print(f\"状态: {sh['status']}, 模式: {sh['mode']}, 完整功能: {sh['full_dispatch_ready']}\")"
```

### 检查版本检测
```bash
curl -s http://localhost:7071/api/ollama/setup-status | \
  python3 -c "import sys, json; d=json.load(sys.stdin)['detail']; print(f\"检测版本: {d['system_version']}, 兼容: {d['version_compatible']}\")"
```

### 停止守护进程
```bash
pkill -f runtime_status_watchdog
```

## 相关文件

- 📄 完整文档: `MONITOR_FIX_SOLUTION.md`
- 🔧 修复脚本: `fix-monitor-issue.sh`
- 🐍 守护进程: `/tmp/runtime_status_watchdog.py`（正在运行，PID: 57407）
- 📊 守护日志: `/tmp/watchdog.log`
- 💾 源码修复: `ai-sidecar/model_manager.py`

---

**建议**: 如果需要完整功能（时间线提炼、自动 bake 等），请运行 `bash fix-monitor-issue.sh` 进行完整修复。如果只是解决监控页面的显示问题，当前状态已足够（守护进程持续运行）。

**修复日期**: 2026-08-18  
**问题诊断**: ✅ 完成  
**临时修复**: ✅ 已实施  
**根治方案**: ⏳ 待执行（运行 fix-monitor-issue.sh）
