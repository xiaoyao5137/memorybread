# 记忆面包 macOS App Store 上架指南

> Owner：个人开发者本人（Individual Account Holder）
> Status：客户端 App Store 构建/上传能力已实现；个人会员、商店材料、税务/银行与商业决策仍需本人完成
> Applies to：通过 Mac App Store 分发的记忆面包客户端
> Audience：个人开发者；可选的外部 TestFlight 测试者或专业顾问
> Last reviewed：2026-08-10
> Next review trigger：App Review Guidelines、沙盒/支付/隐私规则变化，商店拒审或每次大版本发布前

## 先说结论

- **TestFlight 不是提交 App Store 审核的强制前置步骤**。上传并处理完成的构建可以直接选择后提交审核。
- 本项目仍建议把内部 TestFlight 作为上架门禁；需要外部测试者时，再走外部 TestFlight，外测构建可能需要 Beta App Review。
- **App Store 正式上架必须经过 App Review**。Apple 会检查应用功能、内容、元数据、隐私、账号、支付和平台规则，复杂度明显高于官网直装。
- App Store 包使用 Sandbox、商店证书和 provisioning profile，不使用官网包的 Developer ID 分发方式，也不启用 Tauri 自更新。
- 本手册假设会员类型为 **Individual**：注册者本人自动成为唯一 Account Holder，不需要公司、D‑U‑N‑S Number 或组织管理员。
- 个人会员的 App Store 卖家名和 developer name 使用本人法定姓名，不能改成“记忆面包”等品牌名。Apple 系统仍会分配 10 位 Team ID；它只是签名和 profile 的技术标识，不代表多人团队。

Apple 依据：[个人会员注册与卖家名](https://developer.apple.com/help/account/membership/program-enrollment)、[设置 developer name](https://developer.apple.com/help/app-store-connect/create-an-app-record/set-your-developer-name)、[TestFlight 概览](https://developer.apple.com/help/app-store-connect/test-a-beta-version/testflight-overview)、[App Review](https://developer.apple.com/app-store/review/)、[App Review Guidelines](https://developer.apple.com/app-store/review/guidelines/)。

## 1. 目的与范围

本手册覆盖 App Store 上架的完整闭环：账号与商品记录、Sandbox 构建、签名与 PKG、上传、TestFlight 验收、元数据、审核提交、发布和拒审处理。

全部正式操作默认由个人开发者使用自己的 Apple Account 和 Account Holder 权限完成。本手册中的“Team ID”仅指 Apple 分配给个人会员的技术标识，不表示需要创建公司团队。

不包含：

- 官网 DMG、Developer ID、公证、Tauri updater 和直装灰度；参见 [macOS 官网直装发布指南](./macOS官网直装发布指南.md)。
- 个人 Apple Developer Program 注册、身份核验、协议签署或个人税务/银行资料的代办。
- App Store 商品视觉素材的具体设计制作。

## 2. 启动与停止条件

### 启动条件

- 个人 Apple Developer Program 会员有效，Apple Account 已启用双重认证，本人是 Account Holder；中国大陆注册者已按 Apple 要求完成个人身份核验。
- App Store Connect 中适用协议有效；若应用收费或含付费项目，本人的税务和银行资料已经提交并通过相应处理。
- App Store Connect 已创建 macOS App，Bundle ID 精确为 `com.memory-bread.app`。
- Apple Distribution、Mac Installer Distribution 证书与 Mac App Store provisioning profile 可用。
- 本人已经确认隐私、支付、审核账号和服务端测试环境，并把结论写入上架清单。
- 本人已经在带日期的上架清单中确认版本范围、提交语言、目标国家/地区和发布时间。

### 停止条件

出现任一情况立即停止提交：

- Bundle ID、版本号、构建号、证书、profile 或 App Store Connect 记录不一致。
- 修改过客户端内运行/打包的 Python，却未通过 Python 3.9 兼容性门禁。
- Sandbox 构建中仍尝试使用官网 updater、自行下载安装更新或访问未授权路径。
- 审核员无法登录、初始化、获得必要服务端配置或复现核心采集流程。
- 隐私标签、权限说明、加密出口合规、账号删除或付费方案没有确定答案。
- 关键功能依赖审核机上不存在的软件，却未提供可审核的降级路径、说明或演示账号。
- 商店构建、内部 TestFlight 或验收测试存在阻断问题。

## 3. 角色与职责

| 角色 | 职责 | 保留证据 |
|---|---|---|
| 个人开发者（Account Holder） | 独立完成会员、协议、证书、产品范围、隐私、构建、上传、审核回复、发布和生产监控 | 上架清单、App Store Connect 记录、构建与测试日志 |
| 外部 TestFlight 测试者（可选） | 使用外部测试邀请验证真实商店分发包，不接触证书、API key 或 Apple Account | 反馈记录和问题截图 |
| 专业顾问（可选） | 在税务、隐私、支付或规则判断超出个人能力时提供专业意见；最终 App Store 操作仍由本人执行 | 咨询结论；不得包含私钥或真实用户数据 |

本手册默认不邀请其他 App Store Connect 用户。Apple 允许个人会员额外邀请 App Store Connect 用户，但这些人不属于 Apple Developer Program 团队，也没有 Certificates, Identifiers & Profiles 等会员资源权限；如以后启用，应另行定义最小权限。[Apple：账号与角色概览](https://developer.apple.com/help/app-store-connect/manage-your-team/overview-of-accounts-and-roles)

## 4. 前置条件与输入

### 4.1 证书与标识

| 项目 | 要求 |
|---|---|
| 会员身份 | Individual；本人是唯一 Account Holder，法定姓名已核验，不需要 D‑U‑N‑S Number |
| Team ID | Apple 分配给个人会员的 10 位标识，仍须写入签名配置和 profile |
| Bundle ID | `com.memory-bread.app`，须与 App Store Connect、entitlements、profile 完全一致 |
| 应用签名 | 由本人创建并安装在 Keychain 的 Apple Distribution / Mac App Distribution 证书 |
| 安装包签名 | 由本人创建并安装在 Keychain 的 Mac Installer Distribution 证书 |
| Provisioning profile | 与 Bundle ID、Team、Sandbox 权限匹配的 Mac App Store profile |
| 上传凭据 | App Store Connect API Key，拥有上传权限 |
| 系统环境 | 完整 Xcode、项目规定的 Node/Rust/Python 环境 |

### 4.2 App Store Connect 输入

- 唯一应用名称、副标题、主/次分类、年龄分级、版权；developer name/卖家名按个人法定姓名显示，应用名称仍可使用“记忆面包”。
- 中文和目标市场语言的描述、关键词、推广文本。
- 隐私政策、支持页和营销页 HTTPS URL。
- 符合 App Store Connect 当前尺寸要求的 macOS 截图；尺寸以提交页面即时提示为准。
- App Privacy 数据类型、用途、是否关联身份、是否用于追踪。
- 审核联系人、可长期使用的审核账号、初始化步骤、特殊硬件/权限说明。
- 加密出口合规答案。本项目当前 `Info.plist` 声明 `ITSAppUsesNonExemptEncryption=true`，提交时必须根据实际密码学用途回答，不能仅凭字段自动判定。
- 如果应用内销售数字功能或订阅，本人应根据目标地区的当前规则完成 IAP/外链专项判断；不能确定时，先咨询合格专业人士再实现和提交。
- 收费时使用与个人会员身份相符的税务表单和可收款银行账户；具体税务义务由本人确认，不在本手册中推断。

### 4.3 凭据环境变量

```bash
export APPLE_TEAM_ID="<team-id>"
export APPLE_APP_SIGNING_IDENTITY="Apple Distribution: Zhang San (AB12CD34EF)"
export APPLE_INSTALLER_SIGNING_IDENTITY="3rd Party Mac Developer Installer: Zhang San (AB12CD34EF)"
export APPLE_PROVISIONING_PROFILE="/absolute/path/MemoryBread_AppStore.provisionprofile"
export APPLE_API_KEY_ID="<key-id>"
export APPLE_API_ISSUER="<issuer-uuid>"
```

上传脚本要求 API 私钥位于：

```text
~/.appstoreconnect/private_keys/AuthKey_<key-id>.p8
```

真实凭据不得写入 Git、构建日志、应用包或审核说明；只通过 Keychain、CI secret 或受控密码库交接。

证书显示名可能随 Apple 证书类型和账号地区不同；以上姓名与 Team ID 仅为示例。执行 `security find-identity -v -p codesigning` 后，把本机实际 identity 完整复制到环境变量。App Store Connect API 的初次访问申请由个人 Account Holder 本人完成。[Apple：App Store Connect API](https://developer.apple.com/help/app-store-connect/get-started/app-store-connect-api)

## 5. 安全保护、控制与回滚原则

- App Store 构建必须使用 `tauri.appstore.conf.json`、Sandbox entitlements 和商店 profile；不得把官网配置改名后上传。
- App Store 渠道不生成/消费 Tauri updater 产物，更新只能经 App Store。
- Bundle 内所有可执行文件、framework、dylib、helper 和 Python sidecar 都必须由正确身份签名。
- 审核账号只提供完成审核所需的最小权限，不放真实用户数据；审核期间保持有效，审核完成后按策略轮换。
- 被拒版本不覆盖已批准二进制；修复后增加构建号重新上传。
- 已在售版本发生问题时优先停止分阶段发布；需要撤下销售或紧急处理时，由个人开发者在事故记录中写明目标版本、影响和恢复条件后自行确认执行。
- 个人开发者没有组织内复核人时，提交审核和正式发布前必须各做一次独立二次检查：关闭当前工作上下文，重新打开 App Store Connect 清单逐项核验，并保留时间戳。

## 6. 标准上架流程

| ID | 执行人 | 操作 | 预期结果 | 证据 | 失败处理 |
|---|---|---|---|---|---|
| STEP-01 | 个人开发者 | 冻结上架范围、版本、地区、发布时间和商业模式；登记单调递增的构建号 | 发布输入已在个人上架清单中确认 | 带日期的上架清单 | 商业模式或范围未决则停止 |
| STEP-02 | 个人开发者 | 在 `desktop-ui` 执行 `npm run version:set -- <semver> <build>` 和 `npm run version:check` | npm、Tauri、商店版本信息一致 | 命令输出 | 修正后重新执行 |
| STEP-03 | 个人开发者 | 执行受影响测试、Sandbox 权限测试和 Python 3.9 兼容性门禁（若修改 Python）；确认核心功能不依赖任意文件系统权限或官网 updater | 候选提交满足商店运行约束 | CI/测试报告 | 阻断失败则停止 |
| STEP-04 | 个人开发者（Account Holder） | 确认 App ID、证书、installer 证书、profile 和 API key；验证 profile 包含正确 Team/Bundle/entitlements | 个人账号的签名链与上传权限完整 | 证书/profile 清单 | 不匹配转 `EX-01` |
| STEP-05 | 个人开发者 | 创建或更新 App Store Connect 商品记录，填写隐私、年龄分级、加密、价格/可用地区、支持 URL 和元数据 | 所有必填项无缺失且与实际功能、个人卖家身份一致 | 元数据自检记录 | 不确定项转 `EX-04` |
| STEP-06 | 个人开发者 | 注入商店构建凭据，在 `desktop-ui` 执行 `npm run macos:build:appstore` | 生成 Sandbox `.app` 及签名 `.pkg` | 构建日志、产物路径 | 构建/签名错误转 `EX-01` |
| STEP-07 | 个人开发者 | 执行脚本自带 bundle 验证，并检查 `codesign --verify --deep --strict --verbose=2 <app>`、`pkgutil --check-signature <pkg>`、entitlements、Bundle ID、架构和最低系统 | 包结构、签名和权限与商店配置一致 | 验证输出 | 不一致停止上传 |
| STEP-08 | 个人开发者 | 将 API 私钥放入受控路径，执行 `npm run macos:upload:appstore -- <pkg>`；在 App Store Connect 等待处理完成 | 新构建出现在对应版本/build 列表 | 上传日志、构建号 | 处理失败转 `EX-02` |
| STEP-09 | 个人开发者及外部测试者（可选） | 本人先通过内部 TestFlight 验证安装、首次初始化、登录、权限、采集、休眠唤醒、退出和后端交互；需要别人测试时再补齐信息并开启外部 TestFlight | 商店实际分发包通过个人自测或外部验收 | TestFlight 测试记录 | 外测受阻转 `EX-03` |
| STEP-10 | 个人开发者 | 上传最终截图和文案，逐语言预览；确认截图展示真实功能且不含测试数据、内部 URL 或第三方商标问题 | 商品页材料完整一致 | 素材核对表 | 缺失项不得提交 |
| STEP-11 | 个人开发者 | 填写 App Review Information：本人联系方式、演示账号、初始化/权限/采集步骤、需要保持在线的服务、非显而易见功能位置，并亲自按说明试跑 | 审核员可独立完成核心路径 | 审核脚本与账号试跑记录 | 试跑失败转 `EX-04` |
| STEP-12 | 个人开发者（Account Holder） | 完成独立二次检查，选择已验收构建，回答出口合规问卷并运行提交前检查，然后提交 App Review | 状态进入 Waiting for Review/In Review | 二次检查时间、提交时间、版本、构建号 | 被拒转 `EX-05` |
| STEP-13 | 个人开发者 | 审核期间保持账号、配置、API 和依赖服务稳定，监控审核账号但不干预审核行为；及时回复 Resolution Center | 审核可连续完成 | 监控与沟通记录 | 服务异常立即恢复并告知审核 |
| STEP-14 | 个人开发者（Account Holder） | 审核通过后再次独立核对版本和地区，再按个人上架清单选择手动、定时或分阶段发布；验证商店页、下载、登录、采集和更新渠道 | 新版本对目标地区可用 | 二次检查时间、商店链接、生产验收 | 生产异常转 `EX-06` |
| STEP-15 | 个人开发者 | 归档提交元数据、构建提交、上传日志、测试记录、审核通信、批准状态与生产验收 | 上架记录可复现、可审计 | 发布归档链接 | 证据不全不得关闭 |

典型输出位置：

```text
desktop-ui/src-tauri/target/macos-package/appstore/记忆面包_<version>_<arch>.pkg
```

实际文件名以构建命令末尾输出为准。

## 7. TestFlight 决策

| 场景 | 是否需要 TestFlight | 处理方式 |
|---|---|---|
| 只想把构建提交 App Review | Apple 不强制 | 仍须完成本手册其他门禁 |
| 个人开发者本人验收真实商店分发包 | 强烈建议内部 TestFlight | 处理完成后把本人加入内部测试组 |
| 朋友、客户或其他外部人员参与测试 | 使用外部 TestFlight | 准备 Beta 描述、反馈邮箱、测试账号；按 App Store Connect 要求接受 Beta App Review |
| 官网 DMG 测试 | 不使用 | 走官网直装指南和干净 Mac 测试 |

内部 TestFlight 通过不能替代 App Review；Beta App Review 通过也不能替代正式 App Review。

## 8. 审核重点清单

提交前由个人开发者逐项确认；隐私、支付或税务问题无法独立判断时，先取得专业意见再勾选：

- 应用不是空壳、测试版或明显未完成状态，所有展示入口均可工作。
- 审核账号无需短信、人工审批或真实付费即可进入核心流程。
- 首次初始化和采集权限的用途说明清楚，拒绝权限后应用不会崩溃或死循环。
- 菜单栏、后台驻留、开机启动、屏幕/辅助功能等行为均有用户控制和说明。
- 用户可理解收集哪些数据、为何收集、保存多久、如何删除；商店隐私标签与实际行为一致。
- 若支持创建账号，账号删除入口和后端删除流程符合当前 Apple 要求。
- 商店包不显示官网自更新入口，不下载并执行替代应用代码。
- 若依赖 Ollama、本地模型或其他外部组件，审核说明写清安装条件，并提供不依赖审核员自行排障的可验证路径。
- 数字功能、额度、订阅和外链购买方式已经过当前 App Review Guidelines 的专项评审。

## 9. 异常与升级路径

### EX-01: 证书、profile 或 entitlement 不匹配

停止构建/上传。个人开发者用 `security find-identity -v -p codesigning`、profile 解码结果和应用 entitlements 逐项核对本人 Team ID、Bundle ID、Sandbox、App Groups 与能力。修复后增加构建号重打；同一错误连续两次停止自行重试，并查阅 Apple 官方错误说明或联系 Apple Developer Support。

### EX-02: 上传成功但构建处理失败

在 App Store Connect 读取处理错误邮件/状态，核对签名、嵌套二进制、架构、最低系统、版本和加密信息。记录根因并使用新构建号上传，不重复提交相同 PKG。

### EX-03: 外部 TestFlight 无法开放

补齐 Beta App Information、测试说明、账号和合规问卷；如果收到 Beta App Review 反馈，按正式缺陷处理。时间紧时可继续内部测试，但不能把内部测试者伪装成外部用户。

### EX-04: 隐私、支付、依赖或审核路径未决

停止提交。个人开发者记录未决问题；隐私、税务、支付或规则问题交给相应合格专业人士确认，技术问题先在测试环境验证。需要代码或商品模式变更时回到 `STEP-01`；不能在审核备注中用承诺替代实际实现。

### EX-05: App Review 被拒

先保存 Resolution Center 原文和对应条款，复现审核路径并分类：可解释误会、元数据问题、产品缺陷或规则冲突。仅在证据充分时回复说明；需要修改时提升构建号、重新测试并提交。不要反复发送相同解释。

### EX-06: 批准后生产事故

个人开发者立即停止分阶段发布；风险严重时，在事故记录中确认影响和恢复条件后撤下销售。修复版使用更高版本/构建号重新走 TestFlight 与 App Review，官网直装渠道另行处理，不互相覆盖。

### 升级与沟通

| 级别 | 触发条件 | 首要动作 | 通知对象 |
|---|---|---|---|
| P0 | 数据/隐私风险、密钥泄露、恶意行为 | 停止发布或撤下销售，保护账号与数据，并联系 Apple/合格专业支持 | 个人开发者、受影响用户 |
| P1 | 商店用户大面积无法安装、登录或采集 | 停止分阶段发布，准备修复提交和用户说明 | 个人开发者、受影响用户 |
| P2 | 审核拒绝、外测阻塞或局部兼容问题 | 记录条款与证据，设定个人处理期限 | 个人开发者、相关测试者 |

## 10. 验证与收尾

上架只有在以下项目全部通过后才算完成：

- [ ] App Store Connect 的版本、构建号、Bundle ID、地区和价格正确。
- [ ] 商店 `.app`/`.pkg` 的签名、profile、Sandbox entitlements 和架构验证通过。
- [ ] 内部 TestFlight 完成首次安装、初始化、权限、采集和服务端联调验收。
- [ ] 隐私、加密、账号删除、支付和审核说明与实际产品一致。
- [ ] 截图、描述、支持 URL、隐私政策和审核账号可用。
- [ ] App Review 已批准，目标地区可以从 Mac App Store 下载并完成生产验收。
- [ ] 商店版只通过 App Store 更新，不出现官网 updater 行为。
- [ ] 测试、审核通信与发布证据已归档。

## 11. 指标与复审

每次上架至少记录：构建处理成功率、TestFlight 阻断缺陷数、首次审核通过率、各轮审核时长、拒审条款分布、商店安装/首次启动/初始化/采集成功率、崩溃率和支持工单。每次拒审、P0/P1、商业模式变化或 Apple 规则更新后立即复审；大版本发布前至少复审一次。

## 12. 变更记录

| 日期 | 变更 | 作者 |
|---|---|---|
| 2026-08-10 | 改为个人 Apple Developer Program 账号视角，明确法定姓名卖家名、个人 Team ID、Account Holder 权限和单人发布门禁 | Codex |
| 2026-08-09 | 从合并指南拆出 App Store 上架流程，区分可选 TestFlight、Beta App Review 与强制正式 App Review | Codex |
