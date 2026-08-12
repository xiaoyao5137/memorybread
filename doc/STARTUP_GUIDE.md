# 记忆面包 启动指南

## ✅ 系统状态

所有服务已成功启动并通过测试：

- ✅ Core Engine (Rust) - 运行在 http://localhost:7070
- ✅ AI Sidecar (Python) - 后台运行
- ✅ Desktop UI (Tauri + React) - 窗口应用已打开
- ✅ Vite 开发服务器 - 运行在 http://localhost:1420

## 🚀 启动方式

### 方式 1: 完整工作区一键重启（登录与云服务联调推荐）

```bash
cd /path/to/mb-all
./start.sh restart
```

联调、重启后验证、启动应用测试时，默认优先使用 `./start.sh restart`。
这样会先完整停止并清理旧进程，再按顺序重启账户服务、模型网关、运营台和
MemoryBread 客户端，避免因为账户服务未启动而让客户端登录显示网络错误。

在完整 `mb-all` 工作区中直接执行 `MemoryBread/start.sh` 的公开管理命令时，脚本也会自动
委派给上述总启动器。只有明确不需要账户登录与云能力时，才使用
`MEMORYBREAD_LOCAL_ONLY=1 ./start.sh` 仅启动客户端本地组件。

### 方式 2: 客户端单仓启动

当 `MemoryBread` 独立检出，或显式设置 `MEMORYBREAD_LOCAL_ONLY=1` 时，将按顺序启动：
1. AI Sidecar（后台）
2. Model API 与 Creation Service（后台）
3. Core Engine（后台）
4. Desktop UI（后台启动 dev server / Tauri）

该模式不启动 `mb-admin`，因此 Debug 测试环境的本机账户服务
`127.0.0.1:18080` 没有另行启动时，账户登录不可用。

### 方式 3: 手动分步启动

```bash
# 终端 1: AI Sidecar
cd ai-sidecar
source .venv/bin/activate
python main.py

# 终端 2: Core Engine
cd core-engine
cargo run --release

# 终端 3: Desktop UI
cd desktop-ui
npm run tauri:dev
```

## 📋 管理命令

```bash
./start.sh status      # 查看服务状态
./start.sh logs        # 查看实时日志
./start.sh stop        # 停止所有服务
./start.sh restart     # 重启所有服务
./test-system.sh       # 运行系统测试
```

## 🔧 API 端点

### Core Engine (http://localhost:7070)

- `GET /health` - 健康检查
- `GET /api/stats` - 获取统计数据
- `GET /api/vector/status` - 获取向量化状态

### 测试示例

```bash
# 健康检查
curl http://localhost:7070/health

# 查看统计
curl http://localhost:7070/api/stats

# 查看向量化状态
curl http://localhost:7070/api/vector/status
```

## 🐛 调试功能

客户端云端服务地址使用固定的环境映射，不能通过构建变量、历史本地存储或登录页临时覆盖：

| 启动模式 | 环境 | Admin API | Gateway |
| --- | --- | --- | --- |
| Debug（默认） | 测试 | `http://127.0.0.1:18080` | `http://127.0.0.1:18090` |
| Debug（可切换） | 正式 | `https://memorybread.cn` | `https://gateway.memorybread.cn` |
| 非 Debug | 正式（不可切换） | `https://memorybread.cn` | `https://gateway.memorybread.cn` |

每次 Debug 启动都从测试环境开始；关闭调试模式会立即恢复正式环境和正式地址。

在 Desktop UI 的设置页面，点击"🔧 打开调试面板"可以查看：
- 实时采集统计
- 向量化队列状态
- 数据库大小
- 最后采集时间

## 📝 日志位置

所有日志文件存储在：`~/.memory-bread/logs/`

- `sidecar.log` - AI Sidecar 日志
- `core.log` - Core Engine 日志

## ⚠️ 注意事项

1. 首次启动 Tauri 会编译 Rust 代码，需要等待几分钟
2. 确保端口 7070 和 1420 没有被占用
3. macOS 可能会提示安全警告，需要在"系统偏好设置 > 安全性与隐私"中允许运行

## 🎯 下一步

现在您可以：
1. 在 Desktop UI 中配置采集规则
2. 查看实时的采集和向量化状态
3. 通过 API 集成到其他应用

## 🔄 更新代码后

如果修改了代码，需要重新编译：

```bash
# Rust 代码
cd core-engine
cargo build --release

# React 代码（热重载，无需重启）
# Vite 会自动检测变化

# Tauri 配置
# 需要重启 npm run tauri:dev
```

---

**当前版本**: 0.1.0
**最后更新**: 2024-03-04
