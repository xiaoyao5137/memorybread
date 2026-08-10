# 记忆面包 macOS 发布指南索引

更新日期：2026-08-10

原合并手册已经拆成两本独立指南。先按分发入口选择流程，不要混用证书、更新或审核步骤。

## 账号前提：个人开发者

这组指南统一假设你以 **Individual** 类型加入 Apple Developer Program，并由你本人作为 Account Holder 完成证书、协议、构建、上传和发布：

- 个人注册不需要公司或 D‑U‑N‑S Number，但需要本人法定身份、双重认证和有效会员。
- Apple 仍会给个人会员分配 10 位 Team ID；它是代码签名、App ID 和 provisioning profile 使用的技术标识，不等于公司或多人团队。
- Mac App Store 上显示的 seller/developer name 是你的法定姓名，不能改成“记忆面包”等品牌名；应用名称仍可使用“记忆面包”。
- 本指南默认不邀请其他 App Store Connect 用户。外部测试者通过外部 TestFlight 或测试 DMG 参与，不共享你的 Apple Account、证书或 API key。

Apple 依据：[个人会员注册与卖家名](https://developer.apple.com/help/account/membership/program-enrollment)、[个人账号与 App Store Connect 角色](https://developer.apple.com/help/app-store-connect/manage-your-team/overview-of-accounts-and-roles)。

## 两条发布路线

| 项目 | 官网直装 | Mac App Store |
|---|---|---|
| 用户取得应用 | 官网/CDN 下载 DMG | Mac App Store 下载 |
| 主要产物 | `.dmg`；正式更新另含 `.app.tar.gz` 与 `.sig` | 上传 App Store Connect 的签名 `.pkg` |
| 签名方式 | 个人 Account Holder 的 Developer ID Application | 个人 Account Holder 的 Apple Distribution / Mac App Distribution + Mac Installer Distribution |
| Apple 公证 | 正式公开下载需要 | 由商店提交流程处理，不走官网公证链路 |
| TestFlight | 不需要，也不适用 | 上架前非强制；内部验收强烈建议 |
| App Review | 不需要 | 正式上架必须 |
| 应用沙盒 | 依据官网配置 | 必须，使用商店配置和 profile |
| 更新方式 | Tauri updater，消费 `direct` 渠道 | 只能通过 App Store |
| 构建命令 | `npm run macos:build:dmg` | `npm run macos:build:appstore` |

## 选择对应手册

- [macOS 官网直装发布指南](./macOS官网直装发布指南.md)：适合官网公开下载、受控内测 DMG、Developer ID 签名与公证、Tauri 更新和灰度发布。
- [macOS App Store 上架指南](./macOS-App-Store上架指南.md)：适合 App Store Connect、Sandbox、PKG、TestFlight、商品元数据和 App Review。
- [macOS 首次安装初始化测试用例](./macOS首次安装初始化测试用例.md)：两条路线都应执行的干净 Mac 安装、初始化和采集验收。
- [应用内更新与版本管理完整方案](./应用内更新与版本管理完整方案.md)：版本号、直装灰度、商店更新和运营控制面的系统设计。

## 最短决策

- 想把测试 DMG 发给另一台 Mac：走官网直装指南的“内部测试 DMG 快速分支”，不走 TestFlight。
- 想在官网向公众发布：走 Developer ID 签名、公证和 Gatekeeper 验证，不走 App Review。
- 想让用户从 Mac App Store 安装：构建商店 PKG，建议先用内部 TestFlight，最终必须通过 App Review。
- 同时经营官网和商店：使用同一个个人会员和 Team ID 即可，但保持两个渠道的证书/config/profile 独立；官网包不能使用商店 profile，商店包不能启用官网自更新。

官方参考：[Developer ID](https://developer.apple.com/support/developer-id/)、[Apple 公证](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)、[TestFlight](https://developer.apple.com/testflight/)、[App Review](https://developer.apple.com/app-store/review/)。
