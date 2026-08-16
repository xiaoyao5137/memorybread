# DMG 首次启动体验修复验证指南

## 问题描述

**用户报告的症状：**
> "安装后，咨询页报错：AI 能力尚未就绪，未弹出初始化的界面，大概等了几分钟后才弹出来初始化的界面"

**根本问题：**
用户不应该在业务界面看到"AI 能力尚未就绪"错误。无论是首次安装还是已完成初始化的用户，只要 ai-sidecar 还在启动，就应该停留在初始化引导界面等待，而不是进入业务界面。

## 原有逻辑的问题

### 1. App.tsx 的启动逻辑（修复前）

```typescript
// 原逻辑：已完成用户直接放行主界面
const [initializationValidated, setInitializationValidated] = useState(hasCompletedSetup)
const showOnboarding = !initializationValidated || !hasCompletedSetup

// hasCompletedSetup 来自 localStorage: memory-bread_initialization_v2_done
```

**问题场景：**
- **场景 A - 首次安装**：`hasCompletedSetup = false` → 显示 OnboardingWizard ✅
- **场景 B - 已完成用户重启**：`hasCompletedSetup = true` → 直接进入主界面 ❌
  - 如果 sidecar 还在启动（DMG 冷启动需要 2-5 分钟）
  - 用户在咨询页面看到"AI 能力尚未就绪"
  - 用户不知道需要等待什么，也不知道等多久

### 2. OnboardingWizard 的连接超时（修复前）

```typescript
const SIDECAR_RETRY_MS = 1_500    // 1.5秒
const MAX_SIDECAR_RETRIES = 12    // 12次
// 总重试时间：1.5秒 × 12 = 18秒
```

**问题：**
- DMG 版本的 ai-sidecar 冷启动需要 2-5 分钟
- 18 秒后超时，显示"本地服务尚未就绪"错误
- 用户需要手动点击"重新连接"（可能需要多次）

## 修复方案

### 核心思路
**无论是首次安装还是已完成初始化的用户，启动时都必须先在引导界面等待 sidecar 就绪，验证通过后才能进入主界面。**

### 修复 1: 强制初始化检查

```typescript
// desktop-ui/src/App.tsx
// 修复后：所有用户启动时都先显示引导界面
const [initializationValidated, setInitializationValidated] = useState(false)
const showOnboarding = !initializationValidated || !hasCompletedSetup
```

**效果：**
- **所有用户启动时** `initializationValidated = false` → 显示 OnboardingWizard
- OnboardingWizard 连接 sidecar 成功后，调用 `onStatusValidated(true)`
- App.tsx 收到后设置 `initializationValidated = true` → 进入主界面

### 修复 2: 延长连接重试窗口

```typescript
// desktop-ui/src/components/OnboardingWizard.tsx
const SIDECAR_RETRY_MS = 3_000        // 1.5s → 3s
const MAX_SIDECAR_RETRIES = 120       // 12 → 120
// 总重试时间：3秒 × 120 = 360秒 = 6分钟
```

**效果：**
- 6 分钟的重试窗口足够覆盖 DMG 冷启动时间（通常 2-5 分钟）
- 用户无需手动重试

### 修复 3: 智能等待提示

```typescript
const [retryAttempts, setRetryAttempts] = useState(0)

// UI 提示根据重试次数动态变化：
{retryAttempts === 0
  ? '正在启动本地 AI 服务…'
  : retryAttempts < 20
    ? '本地 AI 服务首次启动需要 1-2 分钟，请稍候…'
    : retryAttempts < 60
      ? '仍在启动中，通常在 3 分钟内完成…'
      : '启动时间较长，可能需要 5 分钟，请耐心等待…'
}
```

**效果：**
- 用户清楚知道当前在等什么
- 预期管理更好（知道大概需要多久）

### 修复 4: 自动进入主界面

```typescript
// desktop-ui/src/components/OnboardingWizard.tsx
const applyStatus = useCallback((next: InitializationStatus) => {
  const ready = initializationIsReady(next)
  onStatusValidated?.(ready)
  if (ready) {
    setHasCompletedSetup(true)
    // 初始化完成后自动进入咨询页面（无论是刚完成还是已完成的用户）
    if (previousStateRef.current === 'running' || previousStateRef.current === null) {
      setWindowMode('rag')
    }
  }
  // ...
}, [onStatusValidated, setHasCompletedSetup, setWindowMode])
```

**效果：**
- 首次初始化完成后，自动进入咨询页面
- 已完成用户启动时，sidecar 就绪后自动进入咨询页面
- 无需用户手动操作

## 验证流程

### 场景 A：首次安装（冷启动）

**准备：**
```bash
# 1. 完全卸载
rm -rf /Applications/记忆面包.app
rm -rf ~/Library/Application\ Support/com.memory-bread.app
rm -rf ~/.memory-bread

# 2. 重新构建并安装
cd /Users/xianjiaqi/Documents/mygit/VibeWorking
./build-and-install-dmg.sh

# 3. 安装完成后，打开应用
open /Applications/记忆面包.app
```

**预期行为：**
1. ✅ **立即显示** OnboardingWizard（"烤面包"界面）
2. ✅ 显示提示："正在启动本地 AI 服务…"
3. ✅ 30-60 秒后提示更新为："本地 AI 服务首次启动需要 1-2 分钟，请稍候…"
4. ✅ 2-5 分钟内自动连接成功
5. ✅ 显示"初始化"按钮（此时 sidecar 已就绪）
6. ✅ 点击"初始化"后，开始下载模型和组件（10-30 分钟）
7. ✅ 初始化完成后，自动进入咨询页面

**关键验证点：**
- ❌ **不应该**进入业务界面（咨询页/创作页）
- ❌ **不应该**显示"AI 能力尚未就绪"错误
- ❌ **不应该**需要手动点击"重新连接"

### 场景 B：已完成初始化的用户（热启动）

**准备：**
```bash
# 场景 A 完成后，关闭应用
# 然后重新打开
open /Applications/记忆面包.app
```

**预期行为：**
1. ✅ **先显示** OnboardingWizard（"烤面包"界面）
2. ✅ 显示提示："正在启动本地 AI 服务…"
3. ✅ **几秒钟内**（通常 3-10 秒）自动连接成功
4. ✅ **自动进入**咨询页面（无需用户操作）

**关键验证点：**
- ✅ 即使已完成初始化，启动时仍会**短暂停留**在引导界面
- ✅ 等待时间很短（几秒），用户几乎无感知
- ❌ **不应该**显示"初始化"按钮（已完成用户不需要看到）
- ❌ **不应该**在业务界面显示"AI 能力尚未就绪"

### 场景 C：Sidecar 崩溃后恢复

**准备：**
```bash
# 应用运行中，手动杀死 sidecar
pkill -f "model_api_server.py"

# 等待自动重启（应该由 Tauri 后端自动重启）
```

**预期行为：**
1. 在业务界面使用功能时，可能会遇到错误
2. ✅ 错误提示应该是："AI 正在处理其他任务，请稍候 1-2 分钟再试"
3. ✅ 或："向量模型或推理模型尚未就绪，请前往「模型」界面检查模型状态"

**关键验证点：**
- 这是运行时错误，不是启动问题
- 用户应该能从错误信息中知道怎么处理

### 场景 D：网络问题导致初始化失败

**准备：**
```bash
# 首次安装后，在初始化前断开网络
# 然后点击"初始化"
```

**预期行为：**
1. ✅ 初始化开始，显示进度
2. ✅ 遇到网络错误时，显示明确的错误信息
3. ✅ 提供"重试"按钮
4. ✅ 已下载的组件不会重复下载

## 启动流程对比

### 修复前（有问题）

```
首次安装用户:
  打开 App → localStorage 无完成标记
    → showOnboarding = true
    → 显示 OnboardingWizard
    → 尝试连接 sidecar (重试 18 秒)
    → ❌ 超时失败，显示错误
    → 用户等待几分钟
    → 用户手动点击"重新连接"
    → ✅ 连接成功
    → 点击"初始化"

已完成用户:
  打开 App → localStorage 有完成标记
    → showOnboarding = false
    → ❌ 直接进入业务界面
    → sidecar 还在启动
    → ❌ 业务页面显示"AI 能力尚未就绪"
    → 用户等待几分钟
    → sidecar 启动完成
    → ✅ 业务功能可用
```

### 修复后（正确）

```
首次安装用户:
  打开 App → initializationValidated = false (强制)
    → showOnboarding = true
    → 显示 OnboardingWizard
    → 尝试连接 sidecar (重试 6 分钟)
    → 显示友好的等待提示
    → ✅ 2-5 分钟内自动连接成功
    → 点击"初始化"
    → 10-30 分钟完成初始化
    → ✅ 自动进入咨询页面

已完成用户:
  打开 App → initializationValidated = false (强制)
    → showOnboarding = true
    → 显示 OnboardingWizard
    → 尝试连接 sidecar
    → 显示："正在启动本地 AI 服务…"
    → ✅ 几秒钟内连接成功（热启动快）
    → ✅ 自动进入咨询页面
    → 业务功能立即可用
```

## 代码修改清单

### 1. desktop-ui/src/App.tsx

```typescript
// 修改前：
const [initializationValidated, setInitializationValidated] = useState(hasCompletedSetup)

// 修改后：
const [initializationValidated, setInitializationValidated] = useState(false)
```

**作用：**强制所有用户启动时都先验证 sidecar 可用。

### 2. desktop-ui/src/components/OnboardingWizard.tsx

**修改 A：延长重试时间**
```typescript
const SIDECAR_RETRY_MS = 3_000        // 从 1500
const MAX_SIDECAR_RETRIES = 120       // 从 12
```

**修改 B：添加重试计数**
```typescript
const [retryAttempts, setRetryAttempts] = useState(0)
```

**修改 C：智能等待提示**
```typescript
{connecting && (
  <div className="initialization-connection" role="status">
    <span className="connection-pulse" aria-hidden />
    {retryAttempts === 0
      ? '正在启动本地 AI 服务…'
      : retryAttempts < 20
        ? '本地 AI 服务首次启动需要 1-2 分钟，请稍候…'
        : retryAttempts < 60
          ? '仍在启动中，通常在 3 分钟内完成…'
          : '启动时间较长，可能需要 5 分钟，请耐心等待…'
    }
  </div>
)}
```

**修改 D：自动进入主界面**
```typescript
if (ready) {
  setHasCompletedSetup(true)
  // 初始化完成后自动进入咨询页面（无论是刚完成还是已完成的用户）
  if (previousStateRef.current === 'running' || previousStateRef.current === null) {
    setWindowMode('rag')
  }
}
```

### 3. desktop-ui/package.json

```json
"dependencies": {
  "fflate": "^0.8.2"  // 新增：创作技能压缩依赖
}
```

## 回归测试清单

### 基础功能
- [ ] 首次安装流程完整（引导 → 初始化 → 主界面）
- [ ] 已完成用户启动流程顺畅（引导 → 主界面）
- [ ] 咨询功能正常（模型可用）
- [ ] 创作功能正常（技能加载）
- [ ] 采集功能正常（后台运行）

### 边界情况
- [ ] Sidecar 启动时间超过 3 分钟（仍在重试窗口内）
- [ ] Sidecar 启动时间超过 6 分钟（显示超时错误）
- [ ] 网络断开时初始化（显示网络错误）
- [ ] 磁盘空间不足（显示空间错误）
- [ ] 手动杀死 sidecar（应该自动重启）

### 性能
- [ ] OnboardingWizard 重试过程 CPU 占用 < 5%
- [ ] 内存占用稳定（不泄漏）
- [ ] 已完成用户启动时间 < 10 秒（包括 sidecar 连接）

### 用户体验
- [ ] 等待提示清晰友好
- [ ] 无需用户手动干预
- [ ] 错误信息明确可操作
- [ ] 进度展示合理（不卡顿）

## 技术细节

### initializationIsReady 的判断逻辑

```typescript
// desktop-ui/src/utils/initialization.ts
export function initializationIsReady(status: InitializationStatus): boolean {
  return status.mode === 'normal'
    && status.state === 'completed'
    && status.quality_gate.passed
    && !status.test_mode_enabled
}
```

**条件：**
- `mode === 'normal'`：不是沙盒模式
- `state === 'completed'`：初始化已完成
- `quality_gate.passed`：质检通过
- `!test_mode_enabled`：不是测试模式

### Sidecar 连接重试逻辑

```typescript
const refresh = async () => {
  try {
    const next = await fetchInitializationStatus()
    if (cancelled) return
    applyStatus(next)
    attempts = 0
    setRetryAttempts(0)
  } catch (error) {
    if (cancelled) return
    attempts += 1
    setRetryAttempts(attempts)
    
    if (attempts < MAX_SIDECAR_RETRIES) {
      setConnecting(true)
      timer = setTimeout(refresh, SIDECAR_RETRY_MS)
    } else {
      setConnecting(false)
      setConnectionError('本地初始化服务暂时不可用')
    }
  }
}
```

**特点：**
- 指数退避？❌ 固定间隔（3 秒）
- 最大重试次数：120 次
- 失败后显示错误，提供"重新连接"按钮

## 提交信息

```
commit 731399a
fix: 强制首次启动在初始化界面等待 sidecar 就绪

问题：
- DMG 版本首次启动时，ai-sidecar 冷启动需要 2-5 分钟
- 已完成初始化的用户重启应用时，会直接进入主界面
- 如果 sidecar 还在启动，业务页面会显示"AI 能力尚未就绪"错误
- 新用户在 OnboardingWizard 重试超时（18秒）后需要手动重连

修复策略：
1. 强制初始化检查
   - App.tsx: initializationValidated 初始值改为 false
   - 所有用户启动时都必须先通过 OnboardingWizard 验证 sidecar 可用
   
2. 延长连接重试窗口 (18秒 → 6分钟)
   - SIDECAR_RETRY_MS: 1.5秒 → 3秒
   - MAX_SIDECAR_RETRIES: 12次 → 120次
   
3. 优化等待提示（智能提示不同阶段）
4. 自动进入主界面（已完成用户无需手动操作）

效果：
- ✅ 所有用户启动时在引导界面等待 sidecar 就绪
- ✅ 不会在业务界面看到"AI 能力尚未就绪"错误
- ✅ 无需手动点击"重新连接"
```

## 后续优化建议

### 1. 渐进式启动
分阶段启动服务，允许用户更早使用基础功能：
- Phase 1: 本地存储和采集（最快）
- Phase 2: AI 推理能力（需要 Ollama）
- Phase 3: 云服务（认证、同步）

### 2. 后台服务监控
在 Tauri 后端添加进程监控：
- 自动检测 sidecar 崩溃
- 自动重启异常服务
- 显示准确的服务状态

### 3. 启动性能优化
- 分析 sidecar 启动耗时
- 识别可并行的启动步骤
- 减少冷启动时间到 1 分钟内

### 4. 更好的错误诊断
- 显示 sidecar 日志摘要
- 提供一键诊断工具
- 生成可上报的诊断报告
