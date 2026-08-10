# 记忆面包 macOS 官网直装发布指南

> Owner：个人开发者本人（Individual Account Holder）
> Status：现有构建能力已实现；个人账号的正式证书、更新签名密钥和生产下载地址仍需本人配置
> Applies to：从记忆面包官网、CDN 或个人内测下载页分发的 macOS DMG
> Audience：个人开发者；可选的外部测试者或专业顾问
> Last reviewed：2026-08-10
> Next review trigger：Apple/Tauri 分发规则变化、证书或密钥轮换、发布事故，或每季度复审

## 先说结论

- 官网发布 **不需要 TestFlight**。TestFlight 属于 App Store Connect 的测试分发体系。
- 官网发布 **不经过 App Review**，没有商店截图、商品页和人工产品审核这条链路。
- 正式公开下载仍应使用 **Developer ID 签名 + Apple 公证（notarization）+ stapling**。公证是自动化的恶意软件与签名检查，不是 App Review。
- 因此官网发布明显比 App Store 简单，但不能把未签名或仅 ad-hoc 签名的测试 DMG 直接公开给普通用户。
- 本手册假设 Apple Developer Program 会员类型为 **Individual**。个人开发者仍会获得 10 位 Team ID；这里的 Team ID 只是签名与 profile 使用的技术标识，不代表公司或多人开发团队。

仅给自己或少量受控测试机验证时，可以生成 ad-hoc 测试 DMG；它可能触发 Gatekeeper 拦截，只适合内部测试。公开官网包必须走完整签名与公证流程。

Apple 依据：[Developer ID](https://developer.apple.com/support/developer-id/)、[在 Mac App Store 外分发前进行公证](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)。

## 1. 目的与范围

本手册覆盖以下闭环：生成官网 DMG、生成 Tauri 更新包、签名与公证、干净 Mac 验证、上传不可变下载地址、创建直装渠道发布记录、灰度和回滚。

全部正式操作默认由同一个个人开发者以 Account Holder 身份完成。若邀请别人测试，只交付测试包和测试清单，不共享 Apple Account、证书私钥或 updater 私钥。

不包含：

- App Store Connect、TestFlight 和 App Review；参见 [macOS App Store 上架指南](./macOS-App-Store上架指南.md)。
- 官网页面的视觉和前端实现；本手册只定义下载文件、链接和上线验收。
- 个人 Apple Developer Program 注册、身份核验、隐私政策、网站备案或支付等个人法律事项的代办。

## 2. 启动与停止条件

### 启动条件

同时满足以下条件后开始正式发布：

- 个人 Apple Developer Program 会员有效、Apple Account 已启用双重认证，本人是 Account Holder。
- 本人已经在带日期的发布清单中确认发布范围、版本号、构建号和变更说明。
- 发布分支或提交已冻结，个人自测或外部测试结论可追溯。
- 本人创建的 Developer ID、Apple 公证凭据、Tauri 更新签名密钥均可在受控发布环境中读取。
- DMG、更新包和签名文件拥有 HTTPS 不可变存储位置。
- `mb-ops` 直装渠道发布权限、官网/CDN 发布权限和至少一台干净 Mac 测试机已就绪。

### 停止条件

出现任一情况立即停止，不得继续上传或扩大灰度：

- 版本、构建号、更新记录或目标架构不一致。
- 修改过客户端内运行/打包的 Python，却未通过 Python 3.9 兼容性门禁。
- 正式包没有 Developer ID 签名、公证票据或 Gatekeeper 验证失败。
- `.app.tar.gz`、`.sig`、SHA-256、大小或下载地址之间无法相互校验。
- 首次安装、初始化、权限申请、采集或自动更新的关键路径失败。
- 证书、密钥、令牌或账号疑似泄露。

## 3. 角色与职责

| 角色 | 职责 | 保留证据 |
|---|---|---|
| 个人开发者（Account Holder） | 独立完成范围确认、证书与密钥管理、构建、验证、上传、官网更新、灰度及暂停 | 发布清单、构建日志、产物散列、线上记录 |
| 外部测试者（可选） | 在另一台干净 Mac 上验证安装、初始化、采集、升级和退出；不接触 Apple 或 updater 凭据 | 具名测试记录和问题截图 |
| 专业顾问（可选） | 仅在隐私、税务、安全或事故超出个人能力边界时提供意见 | 咨询结论；不得包含私钥或用户数据 |

## 4. 前置条件与输入

### 4.1 本机构建环境

- macOS 与完整 Xcode，而不是只有 Command Line Tools。
- Node.js/npm、Rust 与项目要求一致。
- 项目所需 Python 环境；若修改了打包内 Python，必须以 Python 3.9 为兼容基线检查。
- 在 `/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/desktop-ui` 执行构建命令。

### 4.2 正式发布凭据

| 用途 | 要求 |
|---|---|
| 应用签名 | 由个人 Account Holder 创建的 `Developer ID Application` 证书与 `APPLE_SIGNING_IDENTITY` |
| Apple 公证 | App Store Connect API Issuer、Key ID 和 `.p8` 私钥 |
| 更新签名 | Tauri updater 私钥和与客户端配置一致的公钥 |
| 文件托管 | HTTPS、不可变版本路径、可读取响应大小与散列 |
| 发布控制 | `mb-ops` 的 `direct` 渠道发布权限 |

环境变量名称以构建脚本为准：

```bash
export APPLE_SIGNING_IDENTITY="Developer ID Application: Zhang San (AB12CD34EF)"
export APPLE_API_ISSUER="<issuer-uuid>"
export APPLE_API_KEY="<key-id>"
export APPLE_API_KEY_PATH="/absolute/path/AuthKey_<key-id>.p8"
export TAURI_SIGNING_PRIVATE_KEY_PATH="/absolute/path/tauri-updater.key"
export MEMORY_BREAD_UPDATER_PUBLIC_KEY="<matching-public-key>"
```

这些值只放在本机 Keychain、CI secret 或受控密码库中；不得写入 Git、DMG、日志、客户端配置或文档示例的真实值。

证书中的姓名和 Team ID 以 `security find-identity -v -p codesigning` 的实际输出为准，复制完整 identity；不要把 `Zhang San` 或示例 Team ID 原样使用。个人 Account Holder 才能创建 Developer ID 证书。[Apple：创建 Developer ID 证书](https://developer.apple.com/help/account/certificates/create-developer-id-certificates/)

## 5. 安全保护、控制与回滚原则

- 正式官网包必须同时具备 Developer ID 签名、公证票据和可验证的 Tauri 更新签名。
- 更新私钥离线或由受控 CI 保管；客户端只能包含公钥。
- 每个版本使用不可变 URL，禁止用新文件覆盖已经发布的同名 URL。
- 直装渠道只消费 `direct` 发布记录，不能让官网构建跳转到 App Store 更新。
- 上线后发现问题先暂停灰度；已经安装的版本采用更高版本号前向修复，不回写或覆盖旧二进制。
- 内部 ad-hoc 包与正式官网包必须在文件名、存储目录和测试记录中明确区分。
- 个人开发者没有组织内复核人时，公开发布前至少间隔一次独立检查：重新下载线上文件，再按本章验证清单逐项复验并保留时间戳；不能用“刚刚构建成功”替代二次确认。

## 6. 标准发布流程

| ID | 执行人 | 操作 | 预期结果 | 证据 | 失败处理 |
|---|---|---|---|---|---|
| STEP-01 | 个人开发者 | 确认版本号与单调递增的构建号；在 `desktop-ui` 执行 `npm run version:set -- <semver> <build>`，随后执行 `npm run version:check` | npm、Tauri 与客户端版本信息一致 | 带日期的个人发布清单、版本检查输出 | 修正版本后重新开始，不复用已上传路径 |
| STEP-02 | 个人开发者 | 执行受影响单元、集成与桌面端测试；修改 Python 时额外执行项目的 Python 3.9 兼容性门禁 | 发布提交通过测试 | CI 链接或本地日志 | 任何阻断失败都停止发布 |
| STEP-03 | 个人开发者（Account Holder） | 在受控终端注入本人 Developer ID、公证和 updater 凭据；确认私钥文件权限 | 凭据可读但未进入仓库和日志 | secret 注入记录 | 凭据缺失或泄露转 `EX-01` |
| STEP-04 | 个人开发者 | 在 `desktop-ui` 执行 `npm run macos:build:dmg` | 生成 DMG、`.app.tar.gz`、`.sig` 和散列信息 | 构建日志、产物目录 | 缺少更新产物转 `EX-02` |
| STEP-05 | 个人开发者 | 验证 `.app` 和 DMG：`codesign --verify --deep --strict --verbose=2 <app>`、`spctl -a -t exec -vv <app>`、`xcrun stapler validate <app>`，并核对构建脚本的 bundle/DMG 校验结果 | 签名、公证、图标、Bundle ID、架构和权限均正确 | 验证命令输出 | Gatekeeper 或票据失败转 `EX-03` |
| STEP-06 | 个人开发者或外部测试者 | 把 DMG 复制到未装过本版本的测试 Mac，挂载后拖入 `/Applications`，从 Finder 首次启动；按测试用例验证初始化、权限、采集、退出与重启 | 用户不需要关闭系统安全策略即可完成关键路径 | [首次安装测试记录](./macOS首次安装初始化测试用例.md) | 关键路径失败则停止发布并记录缺陷 |
| STEP-07 | 个人开发者 | 将 DMG 及 updater 文件上传到版本化 HTTPS 路径；重新下载并核对 SHA-256、字节大小和 `.sig` | CDN 文件与本地产物逐字节一致 | URL、散列、大小、下载日志 | 不一致转 `EX-04` |
| STEP-08 | 个人开发者 | 在 `mb-ops` 创建 `direct` 渠道草稿，填写版本、构建号、架构、最低系统、更新包 URL、SHA-256、大小、签名和发布说明 | 发布记录可以被客户端正确解析 | 草稿截图或 API 响应 | 字段不完整则保持草稿 |
| STEP-09 | 个人开发者 | 将官网主下载按钮指向 DMG URL，并展示版本、支持架构、最低 macOS 和 SHA-256；先在预发布页面验收 | 用户下载的是已验证 DMG，不是 updater 压缩包 | 预发布页面截图与点击测试 | 链接错误立即撤回页面变更 |
| STEP-10 | 个人开发者 | 完成独立二次确认后发布 `direct` 记录，按 1%→10%→25%→50%→100% 或个人发布清单规定的节奏放量；监控下载、安装、启动、更新和崩溃指标 | 指标稳定后完成全量 | 二次确认时间、灰度记录、监控截图 | 异常转 `EX-05` |
| STEP-11 | 个人开发者 | 归档构建提交、命令日志、产物散列、测试结论、线上链接与最终灰度状态 | 发布可复现、可审计 | 发布归档链接 | 证据不全不得关闭发布 |

当前构建脚本的典型输出位置：

```text
desktop-ui/src-tauri/target/<target>/release/bundle/dmg/记忆面包_<version>_<arch>.dmg
desktop-ui/src-tauri/target/<target>/release/bundle/macos/*.app.tar.gz
desktop-ui/src-tauri/target/<target>/release/bundle/macos/*.app.tar.gz.sig
```

实际路径以 `npm run macos:build:dmg` 的末尾输出为准。

## 7. 内部测试 DMG 快速分支

只验证另一台 Mac 的安装与初始化时，可以不配置正式证书和 updater 密钥，直接运行：

```bash
cd /Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/desktop-ui
npm run version:check
npm run macos:build:dmg
```

构建脚本会生成 ad-hoc 测试 DMG，但不会生成可发布的 updater 包。把 DMG 通过 AirDrop、局域网或受控文件分享复制到另一台 Mac，拖到 `/Applications` 后测试。若 Gatekeeper 拦截，可在受控测试机的“系统设置 → 隐私与安全性”中确认来源后手动允许；不得把这种绕过步骤写成官网用户安装说明。

Intel 与 Apple 芯片的架构构建、跨架构限制和测试方法见本指南索引中的架构说明；正式发布应分别在受支持构建环境产出并实机验证，不能把 Rosetta 启动成功等同于原生 Intel 验证。

## 8. 异常与升级路径

### EX-01: 凭据缺失、过期或疑似泄露

停止构建和发布；个人 Account Holder 登录 Apple Developer 与 App Store Connect 确认 Developer ID、公证 API key 与 updater key 状态。疑似 updater/API key 泄露时立即撤销并轮换；Developer ID 私钥疑似泄露时联系 Apple Developer Support 处理证书撤销，并重新构建所有尚未发布的产物。完成书面检查前不得重复尝试。

### EX-02: 没有 `.app.tar.gz` 或 `.sig`

确认 updater 私钥与客户端公钥是否成对设置。只设置其中一项时构建应失败；两项都没有时只允许生成内部测试 DMG。不得创建正式 `direct` 发布记录。

### EX-03: 签名、公证或 Gatekeeper 失败

读取签名与公证日志，修复 entitlement、证书链、时间戳或嵌套二进制问题后提升构建号重打。相同错误连续出现两次，停止自行重试并联系 Apple Developer Support 或查阅官方公证日志说明；不得建议官网用户全局关闭 Gatekeeper 或执行 `xattr -dr`。

### EX-04: CDN 文件与发布元数据不一致

保持发布记录为草稿或立即暂停；删除错误的未公开对象，使用新的不可变版本路径重新上传并从公网复核。若旧 URL 已公开，不覆盖文件，改发更高构建号。

### EX-05: 灰度后出现安装、采集或更新事故

个人开发者立即在 `mb-ops` 暂停放量，并在发布页通知受影响的测试者或用户。保留旧版本下载能力，判断是否需要官网撤下主链接；使用更高版本前向修复，验证通过后重新从小流量开始。

### 升级与沟通

| 级别 | 触发条件 | 首要动作 | 通知对象 |
|---|---|---|---|
| P0 | 密钥泄露、恶意包、广泛数据风险 | 立即停止下载与更新，轮换凭据并联系 Apple/安全专业支持 | 个人开发者、受影响用户 |
| P1 | 大面积无法安装/启动/采集 | 暂停灰度，撤下主链接或回指稳定版 | 个人开发者、已知测试者或用户 |
| P2 | 少量兼容或展示问题 | 保持灰度上限，收集证据并安排修复 | 个人开发者 |

## 9. 验证与收尾

发布只有在以下项目全部通过后才算完成：

- [ ] 版本号、构建号、架构与最低 macOS 一致。
- [ ] 正式应用通过 `codesign`、`spctl` 和 `stapler` 验证。
- [ ] 干净 Mac 从官网链接下载后可以安装、初始化、授权并采集。
- [ ] updater URL、SHA-256、大小和签名匹配，客户端只消费 `direct` 渠道。
- [ ] 官网链接、发布说明、隐私政策和支持入口可访问。
- [ ] 灰度指标稳定，证据归档完整。

## 10. 指标与复审

每次发布至少记录：DMG 下载成功率、安装/首次启动成功率、初始化完成率、采集成功率、更新检查与更新成功率、崩溃率、Gatekeeper/公证相关工单数。出现 P0/P1、证书或密钥轮换、Apple/Tauri 规则变化时立即复审；其余每季度复审一次。

## 11. 变更记录

| 日期 | 变更 | 作者 |
|---|---|---|
| 2026-08-10 | 改为个人 Apple Developer Program 账号视角，统一由 Individual Account Holder 执行并增加单人二次确认 | Codex |
| 2026-08-09 | 从合并指南拆出官网直装发布流程，明确无需 TestFlight/App Review，并补充完整签名、公证、灰度和异常处理 | Codex |
