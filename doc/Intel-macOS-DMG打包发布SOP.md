# 记忆面包 Intel macOS DMG 打包与官网发布 SOP

**Owner：** 个人开发者（Apple Developer Program Account Holder）  
**Status：** 可执行草案；首次 Intel 正式构建和实机验收后转为正式版  
**Applies to：** `MemoryBread` 官网直装渠道、Intel `x86_64` macOS 客户端  
**Audience：** 在另一台 Intel Mac 上执行构建、公证、验证和运营台发布的个人开发者  
**Last reviewed：** 2026-08-15  
**Next review trigger：** 首次 Intel 发布完成、Apple/Tauri 公证规则变化、证书或 updater 密钥轮换、构建脚本变化或发布事故

## 1. 目的与范围

本 SOP 用于在 Intel 芯片 Mac 上，从指定 Git 提交构建原生 `x86_64` 记忆面包客户端，并完成以下闭环：

1. 准备 Intel 原生构建环境。
2. 安全迁移 Developer ID、公证和自动更新凭据。
3. 执行 Python 3.9 兼容性门禁、测试和正式 DMG 构建。
4. 验证架构、Developer ID 签名、Apple 公证和 Gatekeeper。
5. 将 DMG 与自动更新产物录入运营台，形成官网 Intel 下载入口。
6. 从公网重新下载并完成独立复验。

本流程只适用于官网直接分发，不包含 Mac App Store、TestFlight、App Review、App Store provisioning profile 或 Installer 证书。

## 2. 启动条件与停止条件

### 启动条件

同时满足以下条件后开始正式构建：

- Intel Mac 执行 `uname -m` 输出 `x86_64`。
- 已确定本次发布的 Git tag 或完整 commit、版本号和构建号。
- Apple Developer Program 会员有效。
- 已准备 Developer ID Application 签名私钥、公证 API 私钥和既有正式 updater 私钥。
- 拥有运营台发布权限，以及 updater 文件的不可变 HTTPS 托管位置。
- 有一台未安装本版本的 Intel Mac 可进行最终安装测试；构建机本身也可以承担此角色，但必须清理旧版本状态并保留测试证据。

### 立即停止，不得上传或发布

- `uname -m` 或 Python `platform.machine()` 不是 `x86_64`。
- Python 不是 3.9.x，或 Python 3.9 兼容性门禁失败。
- Git 工作区不干净，或无法确认与目标 ARM 版本是否为同一提交。
- Developer ID 只有 `.cer` 而没有对应私钥，`security find-identity` 找不到有效 identity。
- 缺少既有正式 Tauri updater 私钥或与之匹配的公钥。
- 构建日志显示 `ad-hoc 签名，仅供本机测试`。
- 没有同时生成 `.app.tar.gz` 和 `.app.tar.gz.sig`。
- `notarytool` 结果不是 `Accepted`，或 `stapler`、`spctl`、`codesign` 任一验证失败。
- 最终产物中发现 arm64 主程序或 helper，而不是 `x86_64`。
- 生产包启用了 `VITE_MEMORYBREAD_DEBUG_MODE`，或实际请求指向测试环境。
- 公网下载文件与本地产物的 SHA-256 或字节大小不一致。
- 任一私钥疑似进入 Git、构建日志、DMG、公开网盘或聊天记录。

## 3. 角色与职责

| 角色 | 职责 | 保留证据 |
| --- | --- | --- |
| 发布操作者 | 准备环境、导入密钥、构建、公证、验证、上传 | Git commit、命令输出、产物散列、Apple submission ID |
| 发布批准者 | 个人开发者本人在公开发布前进行第二次独立复核 | 带时间戳的发布检查清单 |
| 实机测试者 | 在 Intel Mac 上验证安装、首次启动、初始化、登录、采集与退出 | 测试记录和脱敏截图 |
| 异常升级角色 | 个人开发者本人；证书/公证问题升级至 Apple Developer Support | 工单号或处理记录 |

个人开发者可以同时承担上述角色，但公开发布前必须离开构建现场一段时间后重新下载并复核，不得把“刚构建成功”视为第二次验收。

## 4. 所需设备、工具和访问权限

### 4.1 Intel Mac

- Intel 芯片，终端执行 `uname -m` 为 `x86_64`。
- 安装与该 macOS 版本兼容的完整 Xcode；不能只有 Command Line Tools。
- 能访问 Git、npm、Rust crate、Python package、Apple 公证服务和产物托管地址。
- 建议至少预留 40 GB 可用磁盘空间；首次 npm、Cargo、Python 和 PyInstaller 构建会占用较多空间。

### 4.2 工具链

- 完整 Xcode、`xcodebuild`、`notarytool`、`stapler`、`codesign`、`hdiutil`。
- Node.js 18 或更高版本与 npm。
- Rust stable、Cargo、rustup。
- Intel 原生 Python 3.9.x。
- Git。

### 4.3 权限

- Apple Developer Program Account Holder 权限。
- App Store Connect API Key 的公证权限。
- `mb-ops` 直装渠道发布权限。
- updater 产物的不可变 HTTPS 上传权限。

## 5. 必须安全迁移的凭据

以下材料不得提交 Git，也不得放进项目目录：

| 用途 | 必需材料 | 在 Intel Mac 上的使用方式 |
| --- | --- | --- |
| 应用签名 | Developer ID Application 证书及对应私钥，使用带强密码的 `.p12` 迁移 | 导入登录钥匙串，配置 `APPLE_SIGNING_IDENTITY` |
| Apple 公证 | `AuthKey_<KeyID>.p8`、Key ID、Issuer ID | 配置 `APPLE_API_KEY_PATH`、`APPLE_API_KEY`、`APPLE_API_ISSUER` |
| 自动更新签名 | 当前正式客户端使用的同一把 Tauri updater 私钥；如有密码，同时迁移密码 | 配置 `TAURI_SIGNING_PRIVATE_KEY_PATH` 和可选的 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` |
| 自动更新校验 | 与 updater 私钥匹配的公钥文本 | 配置 `MEMORY_BREAD_UPDATER_PUBLIC_KEY` |

重要限制：

- `.cer` 只包含证书，不包含签名私钥，不能替代 `.p12`。
- CSR 只用于申请证书，构建时不需要。
- Apple Developer ID G2 中间证书通常由系统信任链提供；只有在钥匙串缺少证书链时才需要手动导入。
- 不要把 Apple Silicon Mac 的 `.venv`、`node_modules` 或 `target` 目录复制到 Intel Mac。
- 不要创建一把新的 updater 私钥来替换现有正式私钥；旧客户端只信任已编译进去的既有公钥。
- Apple 公证 API Key 可以使用仍有效的现有 Key，也可以创建一把有权限的新 Key；新建后必须同步新的 Key ID 和 Issuer ID。

### 5.1 从当前 Mac 导出 Developer ID `.p12`

1. 打开“钥匙串访问”。
2. 选择“登录”钥匙串和“我的证书”。
3. 找到 `Developer ID Application: <姓名> (<Team ID>)`。
4. 展开证书，确认其下方显示一把私钥。
5. 同时选择证书和私钥，右键选择“导出 2 个项目”。
6. 保存为 `.p12`，设置高强度导出密码，并把密码保存到密码管理器。
7. 通过加密移动存储、端到端加密密码库或受控局域网通道转移；不得通过普通聊天、邮件附件或公开网盘转移。

### 5.2 Intel Mac 上的文件保护

将 `.p8`、`.p12` 和 updater 私钥放在项目目录之外的受控目录。对私钥执行：

```bash
chmod 600 "/绝对路径/AuthKey_<KeyID>.p8"
chmod 600 "/绝对路径/Developer_ID_Application.p12"
chmod 600 "/绝对路径/tauri-updater.key"
```

导入 `.p12` 后，构建脚本不直接读取 `.p12`；它通过 macOS 钥匙串读取签名 identity。不要把 `.p12` 路径配置为 `APPLE_SIGNING_IDENTITY`。

## 6. 标准操作步骤

| Step | Actor | Action | Expected result | Evidence | Failure path |
| --- | --- | --- | --- | --- | --- |
| 01 | 发布操作者 | 确认 Intel 原生环境与完整 Xcode | 主机、Python 和工具链均满足发布要求 | 预检命令输出 | EX-01 |
| 02 | 发布操作者 | 检出指定发布提交并确认版本策略 | Git 工作区干净，commit、版本和构建号可追溯 | commit SHA、`version:check` 输出 | EX-02 |
| 03 | 发布操作者 | 创建 Intel Python 3.9 虚拟环境并安装依赖 | Python 和 PyInstaller 均为 Intel 可执行文件 | Python 版本、架构和 pip 安装日志 | EX-03 |
| 04 | 发布操作者 | 安装 npm、Rust 依赖并运行发布前测试 | 前端、Rust、Python 受影响测试通过 | 测试日志 | EX-04 |
| 05 | 发布操作者 | 导入 `.p12` 并验证 Developer ID identity | 钥匙串显示有效 Developer ID Application identity | `security find-identity` 输出 | EX-05 |
| 06 | 发布操作者 | 注入 Apple、updater 和生产构建变量 | 所有必需变量已配置，私钥未进入项目或终端历史 | 仅记录变量名已配置，不记录值 | EX-06 |
| 07 | 发布操作者 | 执行 Python 3.9 门禁和 Intel DMG 正式构建 | 生成 DMG、`.app.tar.gz`、`.sig`，且无 ad-hoc 警告 | 构建日志、产物路径 | EX-07 |
| 08 | 发布操作者 | 验证所有 Mach-O 架构和 Developer ID 签名 | 主程序、Rust core、Python helper 均为 `x86_64`，签名有效 | `file`、`codesign` 输出 | EX-08 |
| 09 | 发布操作者 | 验证 App 公证，并提交最终 DMG 公证和 stapling | App 和最终 DMG 均通过票据与 Gatekeeper 验证 | submission ID、`Accepted`、验证输出 | EX-09 |
| 10 | 实机测试者 | 在干净 Intel Mac 上完成安装和关键路径测试 | 普通用户无需绕过 Gatekeeper 即可运行 | 测试清单、脱敏截图 | EX-10 |
| 11 | 发布操作者 | 上传 updater 产物并创建运营台 `x86_64` 草稿 | 发布元数据与本地产物一致，ARM 记录未被覆盖 | URL、SHA-256、字节大小、草稿截图 | EX-11 |
| 12 | 发布批准者 | 完成二次复核并发布 Intel 记录 | 官网显示 Intel 下载入口，客户端能读取对应更新记录 | 发布时间、页面截图、接口结果 | EX-12 |
| 13 | 发布操作者 | 从公网重新下载并复验 | 公网 DMG 和 updater 与本地产物逐字节一致且能安装 | 公网散列、安装结果 | EX-13 |

### STEP-01 — 检查 Intel 主机与 Xcode

执行：

```bash
uname -m
xcodebuild -version
xcrun notarytool --version
xcrun stapler --version
node --version
npm --version
rustc --version
cargo --version
python3.9 -c 'import sys,platform; print(sys.version); print(platform.machine())'
```

成功标准：

- `uname -m` 输出 `x86_64`。
- Python 输出 `3.9.x` 和 `x86_64`。
- `xcodebuild` 能显示完整 Xcode 版本。
- `notarytool`、`stapler`、Node、npm、Rust 和 Cargo 均可执行。

如果 Xcode 尚未初始化，执行：

```bash
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -license accept
sudo xcodebuild -runFirstLaunch
```

### STEP-02 — 检出并冻结发布提交

在 Intel Mac 上克隆仓库后，进入 `MemoryBread` 独立 Git 仓库：

```bash
cd "/绝对路径/mb-all/MemoryBread"
git fetch --tags --prune
git checkout "<发布 tag 或完整 commit>"
git status --short
git rev-parse HEAD
cd desktop-ui
npm run version:check
```

成功标准：

- `git status --short` 无输出。
- commit SHA 与发布清单一致。
- `npm run version:check` 通过。

版本决策：

- 如果这是现有 Apple Silicon 版本的 Intel 补包，必须使用完全相同的 Git commit、版本号和构建号；运营台以架构区分两条记录。
- 如果 Intel 包使用了更新代码，提升版本号和构建号，并为 Apple Silicon 同步构建同版本，避免同一版本在不同架构上行为不一致。

需要设置新版本时：

```bash
cd "/绝对路径/mb-all/MemoryBread/desktop-ui"
npm run version:set -- <semver> <build-number>
npm run version:check
```

版本文件修改必须提交到 `MemoryBread` 仓库后再构建；不要从未提交的工作区制作正式包。

### STEP-03 — 创建 Intel Python 3.9 发布环境

不要复制 Apple Silicon Mac 上的 `.venv`。使用 Intel 原生 Python 3.9 创建：

```bash
cd "/绝对路径/mb-all/MemoryBread/ai-sidecar"
"/绝对路径/python3.9" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ../shared/ipc-protocol/python
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
python -c 'import sys,platform,PyInstaller; print(sys.version); print(platform.machine()); print(PyInstaller.__version__)'
```

成功标准：输出 Python `3.9.x`、架构 `x86_64`，并能加载 PyInstaller。

### STEP-04 — 安装依赖并运行测试

前端：

```bash
cd "/绝对路径/mb-all/MemoryBread/desktop-ui"
npm ci
npm test
npm run build
```

Rust：

```bash
cd "/绝对路径/mb-all/MemoryBread/core-engine"
cargo test --locked
```

Python 测试环境已经包含 pytest 时执行：

```bash
cd "/绝对路径/mb-all/MemoryBread/ai-sidecar"
.venv/bin/python -m pytest -q
```

如项目没有锁定测试依赖，不要为了通过测试擅自提升打包 Python 到 3.10 或更高版本；记录缺失依赖并按 EX-03 处理。

### STEP-05 — 导入并验证 Developer ID

使用“钥匙串访问”把 `.p12` 导入“登录”钥匙串，输入 `.p12` 导出密码。然后执行：

```bash
security find-identity -v -p codesigning
```

必须看到一条有效的：

```text
Developer ID Application: <姓名> (<Team ID>)
```

复制该 identity 的完整文本，后续作为 `APPLE_SIGNING_IDENTITY`。不得使用证书指纹、`.cer` 路径或示例姓名代替。

### STEP-06 — 注入正式发布变量

先确认生产调试变量没有被设置：

```bash
unset VITE_MEMORYBREAD_DEBUG_MODE
```

然后在当前受控终端设置：

```bash
export MEMORY_BREAD_PYTHON_BIN="/绝对路径/mb-all/MemoryBread/ai-sidecar/.venv/bin/python"
export MEMORY_BREAD_PYTHON39_BIN="$MEMORY_BREAD_PYTHON_BIN"

export APPLE_SIGNING_IDENTITY="Developer ID Application: <姓名> (<Team ID>)"
export APPLE_API_ISSUER="<Issuer ID>"
export APPLE_API_KEY="<Key ID>"
export APPLE_API_KEY_PATH="/项目目录之外/AuthKey_<KeyID>.p8"

export TAURI_SIGNING_PRIVATE_KEY_PATH="/项目目录之外/tauri-updater.key"
export MEMORY_BREAD_UPDATER_PUBLIC_KEY="<与正式 updater 私钥匹配的公钥>"
```

如果 updater 私钥有密码，在终端安全读取，不把密码写入脚本或 shell 历史：

```bash
read -r -s -p "Updater key password: " TAURI_SIGNING_PRIVATE_KEY_PASSWORD
echo
export TAURI_SIGNING_PRIVATE_KEY_PASSWORD
```

只检查变量是否存在，不输出真实值：

```bash
for name in APPLE_SIGNING_IDENTITY APPLE_API_ISSUER APPLE_API_KEY APPLE_API_KEY_PATH TAURI_SIGNING_PRIVATE_KEY_PATH MEMORY_BREAD_UPDATER_PUBLIC_KEY; do
  if printenv "$name" >/dev/null 2>&1; then
    echo "$name=configured"
  else
    echo "$name=MISSING"
  fi
done
```

成功标准：六个变量全部显示 `configured`，私钥文件权限为 `600`，且项目目录不存在 `.p8`、`.p12` 或 updater 私钥。

### STEP-07 — 执行正式 Intel 构建

从 `MemoryBread` 根目录执行：

```bash
cd "/绝对路径/mb-all/MemoryBread"
./build-and-install-dmg.sh
```

该入口会先执行客户端 Python 代码的 Python 3.9 兼容性门禁，再根据 Intel 宿主机自动使用 `x86_64-apple-darwin` 构建。

成功标准：

- Python 3.9 兼容性检查通过。
- 日志显示目标为 `x86_64-apple-darwin`。
- 日志不包含 `ad-hoc 签名，仅供本机测试`。
- 生成以下三类产物：

```text
desktop-ui/src-tauri/target/x86_64-apple-darwin/release/bundle/dmg/*.dmg
desktop-ui/src-tauri/target/x86_64-apple-darwin/release/bundle/macos/*.app.tar.gz
desktop-ui/src-tauri/target/x86_64-apple-darwin/release/bundle/macos/*.app.tar.gz.sig
```

同时保留 `.app`：

```text
desktop-ui/src-tauri/target/x86_64-apple-darwin/release/bundle/macos/记忆面包.app
```

### STEP-08 — 验证 Intel 架构和签名

设置产物路径；文件名以构建末尾输出为准：

```bash
APP_PATH="/绝对路径/mb-all/MemoryBread/desktop-ui/src-tauri/target/x86_64-apple-darwin/release/bundle/macos/记忆面包.app"
DMG_PATH="/绝对路径/实际生成的Intel.dmg"
UPDATER_PATH="/绝对路径/实际生成的记忆面包.app.tar.gz"
```

检查主程序和 helpers：

```bash
file "$APP_PATH/Contents/MacOS/memory-bread-desktop"
file "$APP_PATH/Contents/MacOS/memory-bread-core"
file "$APP_PATH/Contents/Helpers/memory-bread-ai.app/Contents/MacOS/memory-bread-ai"
```

三项都必须包含 `x86_64`，不得只显示 `arm64`。

检查签名：

```bash
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
codesign -d --verbose=4 "$APP_PATH" 2>&1 | grep -E 'Authority=Developer ID Application|TeamIdentifier=|Timestamp=|Runtime Version='
spctl -a -t exec -vv "$APP_PATH"
hdiutil verify "$DMG_PATH"
```

成功标准：`codesign` 和 `hdiutil` 返回 0；`spctl` 显示 accepted，签名 Authority 为预期 Developer ID，具有安全时间戳和 Hardened Runtime。

### STEP-09 — 验证 App，并公证最终 DMG

先验证 Tauri 构建后的 App 是否已经获得公证票据：

```bash
xcrun stapler validate "$APP_PATH"
```

如果失败，不得只公证外层 DMG 后继续发布；按 EX-09 停止并检查 Tauri 公证日志。

项目构建脚本会在 Tauri 构建结束后给外层 DMG 添加 Finder 文件图标，因此无论此前日志是否出现公证成功，都对最终 `DMG_PATH` 再提交一次：

```bash
xcrun notarytool submit "$DMG_PATH" \
  --key "$APPLE_API_KEY_PATH" \
  --key-id "$APPLE_API_KEY" \
  --issuer "$APPLE_API_ISSUER" \
  --wait
```

只在结果明确为 `Accepted` 后执行：

```bash
xcrun stapler staple "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"
spctl -a -t open --context context:primary-signature -vv "$DMG_PATH"
```

保留 notary submission ID、最终状态和 stapler/spctl 输出。公证不是 App Review；官网直装版本不需要提交 TestFlight 或等待人工 App Review。

### STEP-10 — Intel 实机安装测试

1. 将最终已 stapled 的 DMG 复制到未安装本版本的 Intel Mac。
2. 双击挂载 DMG，把“记忆面包”拖入 `/Applications`。
3. 从 Finder 启动，不能使用 `xattr -dr`、全局关闭 Gatekeeper或“仍要打开”作为正式验收步骤。
4. 验证首次启动、权限引导、初始化、登录、生产环境访问、采集、咨询、退出和重启。
5. 打开“活动监视器”或执行 `file`，确认进程不是通过非预期兼容模式运行。

生产环境检查：

- 账号服务请求应访问 `https://memorybread.cn`。
- 网关请求应访问 `https://gateway.memorybread.cn`。
- 不得访问 `127.0.0.1:18080` 或 `127.0.0.1:18090` 作为云端服务环境；客户端本地 core/sidecar 使用的回环地址不属于测试环境问题。

### STEP-11 — 准备运营台元数据并创建草稿

先计算本地产物元数据：

```bash
shasum -a 256 "$DMG_PATH"
stat -f '%z' "$DMG_PATH"
shasum -a 256 "$UPDATER_PATH"
stat -f '%z' "$UPDATER_PATH"
cat "$UPDATER_PATH.sig"
```

`.sig` 是公开签名值，不是 updater 私钥。绝对不要上传 `tauri-updater.key`。

将 `.app.tar.gz` 和对应 `.sig` 上传到不可变、版本化 HTTPS 路径。不得用 Intel 文件覆盖 Apple Silicon 的同名 URL。

在运营台创建一条新的发布草稿：

- 平台：`macOS`
- 架构：`x86_64`
- 分发渠道：`direct`
- 发布频道：`stable`，或本次批准的灰度频道
- 版本号和构建号：与 App 完全一致
- 最低系统版本：`12.0`
- Installer：最终 Intel DMG
- Updater URL：Intel `.app.tar.gz` 的不可变 HTTPS URL
- Updater SHA-256：本地计算值
- Updater 字节大小：本地计算值
- Updater signature：`.app.tar.gz.sig` 文件内容
- 发布说明、灰度比例和发布理由

不要编辑或覆盖现有 `aarch64` 记录。只上传 DMG 而没有 updater URL、散列、大小和签名，不算完整正式发布。

### STEP-12 — 二次复核并发布

发布批准者重新核对：

- Git commit、版本、构建号和 `x86_64` 架构。
- Apple App 和最终 DMG 的公证/票据验证证据。
- DMG 与 updater 的 SHA-256、大小、签名和 URL。
- 运营台草稿没有覆盖 `aarch64` 记录。
- 官网 Intel 按钮最终指向 DMG，而不是 `.app.tar.gz`。

全部通过后发布 `x86_64` 记录。官网分发不需要再走 Apple App Review；Apple 侧的终点是公证 `Accepted` 和 stapling 验证通过。

### STEP-13 — 公网复验与收尾

从官网实际下载 Intel DMG，并从 updater URL 下载 `.app.tar.gz`：

```bash
shasum -a 256 "/公网重新下载的Intel.dmg"
shasum -a 256 "/公网重新下载的记忆面包.app.tar.gz"
```

成功标准：

- 公网散列和字节大小与运营台记录及本地产物完全一致。
- 官网 Apple Silicon 和 Intel 两个下载入口各自下载正确架构。
- 公网 DMG 重新通过 `hdiutil verify`、`stapler validate` 和实际安装。
- 客户端更新检查返回 `x86_64` 对应的 updater 记录。

发布完成后归档：commit SHA、版本、构建号、构建日志、notary submission ID、产物散列、运营台记录、官网链接和测试结果。归档不得包含任何私钥、密码、完整 API Key 或用户数据。

## 7. 决策点与异常处理

### EX-01 — 主机、Xcode 或工具链不满足条件

- **Signal：** 架构不是 `x86_64`，只有 Command Line Tools，或 `notarytool`/构建工具缺失。
- **Retry limit：** 修复后重试一次预检。
- **Fallback：** 更换可运行完整 Xcode 的 Intel Mac。
- **Stop condition：** 不能用 Apple Silicon 交叉编译代替；当前 Python sidecar 构建脚本只支持宿主架构。
- **Escalate to：** 个人开发者；Xcode 安装或账号问题升级至 Apple Developer Support。

### EX-02 — Git、版本或构建号不确定

- **Signal：** 工作区有未提交修改、commit 不明、相同版本准备打入不同代码。
- **Retry limit：** 不直接重试构建。
- **Fallback：** 回到发布清单确定 tag/commit；新代码提升版本与构建号。
- **Stop condition：** 无法确认来源时不得构建正式包。
- **Escalate to：** 发布批准者。

### EX-03 — Python 3.9 或依赖安装失败

- **Signal：** Python 不是 3.9、架构不是 `x86_64`，或依赖已停止支持 Python 3.9。
- **Retry limit：** 使用干净 `.venv` 重试一次。
- **Fallback：** 使用上一正式发布记录的兼容依赖版本，在 Intel Python 3.9 中重新安装；不得复制 ARM `.venv`。
- **Stop condition：** 不得通过切换到 Python 3.10+ 绕过发布门禁。
- **Escalate to：** 个人开发者，并补充依赖锁定后再发布。

### EX-04 — 测试失败

- **Signal：** npm、Rust、Python 受影响测试或前端构建失败。
- **Retry limit：** 排除网络或缓存原因后重试一次。
- **Fallback：** 修复代码并提交新 commit，重新从 STEP-02 开始。
- **Stop condition：** 不得用跳过测试构建正式公开包。
- **Escalate to：** 发布批准者。

### EX-05 — 钥匙串没有有效签名 identity

- **Signal：** `security find-identity` 找不到 Developer ID Application，或只导入了 `.cer`。
- **Retry limit：** 重新导入 `.p12` 一次。
- **Fallback：** 在当前持有私钥的 Mac 重新导出 `.p12`；或在 Intel Mac 生成新 CSR 并由 Account Holder 创建新 Developer ID Application 证书。
- **Stop condition：** 不得使用 ad-hoc 包公开发布。
- **Escalate to：** Apple Developer Support。

### EX-06 — 公证或 updater 凭据缺失

- **Signal：** 任一必需变量未配置，`.p8` 无法读取，updater 私钥/公钥不匹配。
- **Retry limit：** 修正配置后重试一次变量预检。
- **Fallback：** 从受控密码库恢复；公证 API Key 可新建，但 updater 私钥不能随意轮换。
- **Stop condition：** 丢失正式 updater 私钥时停止发布，先设计兼容的密钥迁移方案。
- **Escalate to：** 发布批准者；Apple API 问题升级至 Apple Developer Support。

### EX-07 — 只生成测试 DMG 或缺少 updater 产物

- **Signal：** 日志出现 ad-hoc 警告，或没有 `.app.tar.gz/.sig`。
- **Retry limit：** 修正凭据后完整重构一次。
- **Fallback：** 该 DMG 只能标记为内部测试包，不得进入运营台正式记录。
- **Stop condition：** 正式发布字段不完整。
- **Escalate to：** 发布批准者。

### EX-08 — 架构或签名失败

- **Signal：** 任一主程序/helper 为 arm64，或 `codesign`/`spctl` 失败。
- **Retry limit：** 清理目标产物并完整重构一次。
- **Fallback：** 重建 Intel 原生 Python 环境、Cargo target 和 npm 依赖。
- **Stop condition：** 不得把 Rosetta 可运行视为原生 Intel 验收。
- **Escalate to：** 个人开发者。

### EX-09 — Apple 公证失败

- **Signal：** App 无票据、DMG 状态不是 `Accepted`，或 stapler/spctl 失败。
- **Retry limit：** 同一未修改二进制最多重新查询或提交一次；不要连续盲目重提。
- **Fallback：** 使用 submission ID 获取公证日志，修复 entitlement、Hardened Runtime、嵌套签名、证书链或时间戳后提升构建号重打。
- **Stop condition：** 不得建议官网用户关闭 Gatekeeper 或执行 `xattr -dr`。
- **Escalate to：** Apple Developer Support。

### EX-10 — Intel 实机关键路径失败

- **Signal：** 无法首次启动、初始化、登录、采集、咨询或正常退出。
- **Retry limit：** 排除临时网络原因后重试一次。
- **Fallback：** 保留旧版，修复并提升构建号重新发布。
- **Stop condition：** 不得上传或扩大灰度。
- **Escalate to：** 发布批准者。

### EX-11 — 运营台元数据不完整或不一致

- **Signal：** URL、SHA-256、大小、签名、架构或版本不一致。
- **Retry limit：** 草稿状态可修正一次。
- **Fallback：** 使用新的不可变 URL 重新上传；不要覆盖已经公开的旧文件。
- **Stop condition：** 记录保持草稿，不得发布。
- **Escalate to：** 发布批准者。

### EX-12 — 官网指向错误或覆盖 ARM 记录

- **Signal：** Intel 按钮下载 ARM 包、按钮指向 updater 压缩包，或 ARM 入口消失。
- **Retry limit：** 立即暂停或撤回新记录，不反复覆盖线上文件。
- **Fallback：** 恢复已知稳定记录，修正独立的 `x86_64` 发布数据。
- **Stop condition：** 链接正确前不得继续灰度。
- **Escalate to：** 发布批准者。

### EX-13 — 公网文件与本地不一致

- **Signal：** SHA-256、字节大小、签名或实际架构不一致。
- **Retry limit：** 清除本地下载缓存后重新下载一次。
- **Fallback：** 暂停运营台记录，使用新的不可变 URL 重新上传并复验。
- **Stop condition：** 不覆盖已公开 URL，不让用户继续下载可疑文件。
- **Escalate to：** 发布批准者；疑似篡改时按安全事件处理。

## 8. 升级与沟通

- **P0：私钥泄露、产物疑似被篡改或错误包已公开。** 发现后立即暂停运营台记录和官网入口，个人开发者开始保存脱敏证据并执行第 9 节安全事件流程；涉及 Apple 凭据时立即联系 Apple Developer Support。
- **P1：已公开的 Intel 包无法安装、启动、初始化或更新。** 发现后立即停止灰度，官网回指上一个已验证版本；在完成影响范围确认前不得覆盖旧 URL。
- **P2：发布前构建、测试、公证或元数据失败。** 保持运营台草稿，不通知普通用户；在下一次重试前记录失败阶段、错误摘要和采用的修复。
- 对外沟通只包含受影响版本、架构、症状、临时措施和下一次更新时间，不包含密钥、用户数据、完整公证日志或内部服务凭据。
- 个人开发者同时承担操作者和批准者时，仍需在公开发布前完成带时间戳的独立二次复核。

## 9. 密钥泄露与安全事件

出现下列任一情况时立即停止构建和发布：

- `.p8`、`.p12`、updater 私钥或密码进入 Git/日志/聊天/公开存储。
- Intel Mac 丢失、被未授权访问或恶意软件感染。
- 公网产物散列异常或签名身份异常。

处置顺序：

1. 暂停运营台发布和官网入口。
2. 撤销受影响的 Apple API Key；Developer ID 私钥疑似泄露时联系 Apple 处理证书撤销。
3. updater 私钥疑似泄露时停止自动更新发布，设计客户端信任迁移方案；不得直接用新 key 覆盖。
4. 保存脱敏证据和时间线，不复制私钥内容进入事故记录。
5. 使用新构建号重建所有尚未发布的产物。

## 10. 验证与收尾清单

- [ ] `uname -m`、Python 和全部 Mach-O 均确认 `x86_64`。
- [ ] Git commit、版本号和构建号可追溯，工作区干净。
- [ ] Python 3.9 兼容性门禁通过。
- [ ] 没有 ad-hoc 签名警告。
- [ ] `.app` 通过 `codesign`、`spctl` 和 `stapler validate`。
- [ ] 最终 DMG 的 Apple 公证结果为 `Accepted`，并通过 stapler、spctl 和 hdiutil。
- [ ] DMG、`.app.tar.gz` 和 `.sig` 齐全。
- [ ] Intel 实机首次安装和生产环境关键路径通过。
- [ ] 运营台创建独立 `x86_64/direct` 记录，没有覆盖 `aarch64`。
- [ ] 公网重新下载后的 SHA-256 和字节大小一致。
- [ ] 官网 Intel 与 Apple Silicon 下载按钮均正确。
- [ ] 构建证据已归档，归档中没有私钥和密码。

## 11. 指标与复审

每次 Intel 发布至少记录：

- 构建成功/失败及失败阶段。
- Apple 公证时长与失败原因。
- 公网 DMG 下载和散列复验结果。
- Intel 首次安装、初始化、登录和采集成功情况。
- Intel 自动更新检查和更新成功情况。
- Gatekeeper、公证和崩溃相关反馈。

发生证书或密钥轮换、Apple/Tauri 规则变化、构建脚本变化、P0/P1 发布事故时立即复审本 SOP；无事件时每季度复审一次。

## 12. 参考资料

- 项目官网直装总流程：[macOS官网直装发布指南.md](./macOS官网直装发布指南.md)
- Intel/ARM 构建入口：[build-macos.sh](../scripts/build-macos.sh)
- Python 3.9 门禁与构建入口：[build-and-install-dmg.sh](../build-and-install-dmg.sh)
- Apple Developer ID：<https://developer.apple.com/support/developer-id/>
- Apple 公证说明：<https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution>
- Apple 签名身份迁移：<https://developer.apple.com/documentation/Xcode/sharing-your-teams-signing-certificates>
- Tauri macOS 签名与公证：<https://v2.tauri.app/distribute/sign/macos/>

## 13. 变更记录

- 2026-08-15 — 创建 Intel `x86_64` 独立构建、公证、运营台发布与异常处理 SOP。
