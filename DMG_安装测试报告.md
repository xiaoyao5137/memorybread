# 记忆面包 DMG 安装测试报告

**版本**: 0.1.0  
**架构**: x86_64  
**测试日期**: 2026-08-05  
**测试环境**: macOS 12.x

---

## 📦 构建产物

### DMG 文件
- **路径**: `desktop-ui/src-tauri/target/x86_64-apple-darwin/release/bundle/dmg/记忆面包_0.1.0_x64.dmg`
- **大小**: 279 MB（压缩后）
- **格式**: UDZO (zlib 压缩)
- **校验**: ✅ Checksummed

### App Bundle
- **路径**: `desktop-ui/src-tauri/target/x86_64-apple-darwin/release/bundle/macos/记忆面包.app`
- **大小**: 928 MB（解压后）
- **签名**: adhoc（适合本地测试）
- **Bundle ID**: com.memory-bread.app

---

## 🔧 集成组件验证

### 1. Desktop UI (Tauri)
- ✅ 主可执行文件: `memory-bread-desktop` (15 MB)
- ✅ 前端资源: React + TypeScript 打包
- ✅ 图标资源: icon.icns

### 2. Core Engine (Rust)
- ✅ 二进制文件: `memory-bread-core` (18 MB)
- ✅ 编译目标: x86_64-apple-darwin
- ✅ Release 优化

### 3. AI Sidecar (Python)
- ✅ PyInstaller 打包: `Contents/Helpers/memory-bread-ai.app`
- ✅ Python 运行时: Python 3.9.8
- ✅ 依赖库:
  - torch (PyTorch)
  - transformers (HuggingFace)
  - sentence-transformers (向量编码)
  - Flask (API 服务)
  - SQLite (数据迁移)
- ✅ 迁移脚本: `migrations/*.sql`

---

## 🚀 安装测试

### 步骤 1: DMG 挂载
```bash
hdiutil attach 记忆面包_0.1.0_x64.dmg
```
- ✅ 自动挂载到 `/Volumes/记忆面包`
- ✅ 包含 Applications 符号链接
- ✅ 卷图标正确显示

### 步骤 2: 应用安装
```bash
cp -R "/Volumes/记忆面包/记忆面包.app" /Applications/
```
- ✅ 复制成功
- ✅ 权限保留

### 步骤 3: 应用启动
```bash
open -a "/Applications/记忆面包.app"
```
- ✅ 应用成功启动
- ✅ 无 Gatekeeper 警告（adhoc 签名）

---

## ✅ 功能验证

### 进程检查
```
xianjiaqi  87596  /Applications/记忆面包.app/Contents/MacOS/memory-bread-desktop
xianjiaqi  87614  /Applications/记忆面包.app/Contents/MacOS/memory-bread-core
xianjiaqi  87612  /Applications/记忆面包.app/Contents/Helpers/memory-bread-ai.app/Contents/MacOS/memory-bread-ai model-api
xianjiaqi  87613  /Applications/记忆面包.app/Contents/Helpers/memory-bread-ai.app/Contents/MacOS/memory-bread-ai creation
```
**结论**: ✅ 所有核心进程正常运行

### 服务端口监听
- ✅ Core Engine: `127.0.0.1:7070` (LISTEN)
- ✅ AI Sidecar: `127.0.0.1:7071` (LISTEN)

### API 健康检查
```bash
# Core Engine Health
curl http://127.0.0.1:7070/health
# {"status":"ok","version":"0.1.0"}
```
**结论**: ✅ Core Engine 正常

```bash
# AI Sidecar Health
curl http://127.0.0.1:7071/health
# {"active_embedding":"bge-small-zh","active_llm":"mbem-v1-local","pipeline_ready":true,"status":"ok"}
```
**结论**: ✅ AI Sidecar 正常

### 模型管理 API
```bash
curl http://127.0.0.1:7071/api/models
```
**返回**: 完整的模型列表（MBEM v1.0, MBEMB V1.0, 商业 API 等）  
**结论**: ✅ 模型管理模块正常

---

## 🎯 测试结果汇总

| 测试项 | 状态 | 备注 |
|--------|------|------|
| DMG 构建 | ✅ 通过 | 279 MB, 压缩良好 |
| DMG 挂载 | ✅ 通过 | 自动挂载，结构正确 |
| 应用安装 | ✅ 通过 | 复制到 Applications 成功 |
| 代码签名 | ✅ 通过 | adhoc 签名，本地测试可用 |
| 应用启动 | ✅ 通过 | 无错误，进程正常 |
| Desktop UI | ✅ 通过 | 主进程运行 |
| Core Engine | ✅ 通过 | 端口 7070 监听，API 响应 |
| AI Sidecar | ✅ 通过 | 端口 7071 监听，模型 API 正常 |
| Python 依赖 | ✅ 通过 | PyInstaller 打包完整 |
| 模型管理 | ✅ 通过 | 模型列表正确返回 |

---

## 📝 建议和改进

### 1. Bundle Identifier 警告
```
Warn: The bundle identifier "com.memory-bread.app" ends with `.app`
```
**建议**: 修改为 `com.memorybread.app` 或 `com.memory-bread.desktop`

### 2. 前端代码分割
```
(!) Some chunks are larger than 500 kB after minification
```
**建议**: 使用 dynamic import() 或 manualChunks 优化

### 3. Developer ID 签名（可选）
- 当前使用 adhoc 签名，适合本地测试
- 如需分发，建议申请 Apple Developer ID 证书
- 需要配置环境变量:
  - `APPLE_SIGNING_IDENTITY`
  - `APPLE_ID` / `APPLE_PASSWORD` / `APPLE_TEAM_ID`

### 4. 首次引导流程
- 应用已启动，但未测试 OnboardingWizard
- 建议手动测试：
  1. 硬件检测
  2. LLM 模型选择
  3. Embedding 模型选择

---

## 🎉 总体结论

**✅ DMG 打包和安装测试完全通过**

- 构建流程完整：Rust core-engine ✅, Python ai-sidecar ✅, React desktop-ui ✅
- 所有核心组件成功集成到单一 App Bundle
- 安装流程顺畅，无需手动配置依赖
- 所有后台服务自动启动并正常运行
- API 服务验证通过

**可以进行下一步开发或用户测试！**
