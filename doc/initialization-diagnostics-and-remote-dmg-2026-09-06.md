# 初始化故障修复与远程诊断 DMG

## 本次范围

- 主咨询和悬浮咨询共用 `/api/initialization/readiness`：检查正式初始化完成、托管引擎存活、模型存在及咨询管线就绪。页面允许输入；不可用时不提交咨询，提供主界面修复入口。模型恢复后后台单次预热并按间隔重试，避免门禁阻断恢复。
- 引擎下载错误区分网络、HTTP、TLS、超时、续传、校验、磁盘写入、解包和安装。稳定错误码仍兼容现有服务端；不扩展服务端严格白名单的检查项 ID。
- 每个下载源使用独立缓存（源与预期 SHA256 共同确定身份）。仅持有 ETag 或 Last-Modified 的部分文件尝试 If-Range 续传，核对 Content-Range；服务端忽略 Range 时覆盖写入，416 或不一致续传清理该源缓存。校验失败继续下一个源；网络失败保留缓存；完整已校验缓存可直接复用。
- 下载每源最多三次、每源预算五分钟、每轮预算十五分钟，单次网络等待最多十五秒。原有阶段自动修复最多再执行一轮。时间预算在连接和读取时执行，不能中断操作系统阻塞的文件 I/O。
- 每次下载尝试仅记录源编号、尝试编号、稳定错误码、HTTP 状态和耗时。结构化报告的现有 summary 带各源最后一次失败；完整尝试信息进入服务日志。不向客户端或云端报告加入供应商 URL、凭据或用户内容。
- 结构化诊断收到后立即保存待补传回执，日志失败不显示完整成功；重新打开初始化页可直接补传，复用报告编号。桌面上传继续使用 Tauri 原生命令，不回退 WebView PUT；增加超时与禁止重定向。
- DMG 校验增加实际 helper 的 `diagnostics-self-check`，并检查桌面二进制包含原生日志上传命令。此门禁只能证明功能进入包，真实客户网络和 OSS 上传仍需要安装版验收。

## 如何生成一个给客户排查用的 DMG

本次按“如何打包”提供流程，没有生成、安装、发送或发布新的 DMG。当前工作区还有其他未提交改动；下面的命令会打入整个当前工作区，不是仅打入本次修复。正式交付前应确认版本和待发布内容。

构建机与客户架构必须匹配：Apple Silicon 使用 arm64 包，Intel 使用 x86_64 包。现有构建脚本只支持宿主架构，不会自动生成通用包。Python 发布环境必须为 3.9。

在终端运行：

```bash
cd /Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/desktop-ui
export PATH="$HOME/.cargo/bin:$PATH"
npm run version:check
# 分发前设置可辨认、尚未使用的版本号和递增构建号：
# npm run version:set -- <版本号> <构建号>
npm run macos:build:dmg
```

如果默认 `ai-sidecar/.venv/bin/python` 不是 Python 3.9，可通过 `MEMORY_BREAD_PYTHON_BIN` 指定已准备好依赖的 Python 3.9 环境。脚本会一起构建 Rust 核心服务、Python helper、前端和 Tauri 桌面壳；不要仅运行前端 build 或拿旧 DMG 重命名。

构建末尾会打印 App 和 DMG 的绝对路径。通常位于：

```text
MemoryBread/desktop-ui/src-tauri/target/<target>/release/bundle/dmg/
```

## 让对方尽量直接安装

要让客户正常安装，使用 Developer ID 签名并完成 Apple 公证。Apple 的说明：[公证工作流](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)。

在上面的构建命令之前设置实际签名身份（可用 `security find-identity -v -p codesigning` 查看）；公证凭据使用已有的钥匙串 profile 或项目发布流程，不能把示例文字直接当凭据。

```bash
export APPLE_SIGNING_IDENTITY='Developer ID Application: 实际证书身份'
npm run macos:build:dmg

# 使用构建日志中实际打印的最终文件路径：
DIAGNOSTIC_DMG='/绝对路径/记忆面包_版本_架构.dmg'
xcrun notarytool submit "$DIAGNOSTIC_DMG" --keychain-profile '已有公证凭据名称' --wait
# 仅在上一步显示 Accepted 后执行：
xcrun stapler staple "$DIAGNOSTIC_DMG"
xcrun stapler validate "$DIAGNOSTIC_DMG"
spctl --assess --type open --context context:primary-signature --verbose=2 "$DIAGNOSTIC_DMG"
shasum -a 256 "$DIAGNOSTIC_DMG"
```

当前脚本会重建最终 DMG，所以不能仅凭 Tauri 中间产物公证过就推断最终 DMG 已完成公证。务必验证最终发送的文件。若已经配置了 Tauri 更新签名，脚本还会生成更新产物；只给这一位用户手动安装不需要创建官网发布记录。

没有 Developer ID 时，脚本会生成 ad-hoc 测试包，不能承诺客户双击直接打开。受控测试可由客户确认来源后在“系统设置 → 隐私与安全性”中允许；不要求关闭系统安全机制。[Apple：安全打开应用](https://support.apple.com/102445)

## 可以发送给客户的操作说明

> 请先退出正在运行的记忆面包，然后打开收到的 DMG，将“记忆面包”拖到 Applications，提示替换时确认替换。请从“应用程序”启动，不要直接从 DMG 卷里运行。
>
> 不要删除原来的应用数据、初始化目录或日志，这些是定位问题需要的证据。进入后如果仍然提示初始化失败，请先点击“上报诊断”，确认上报；若显示“诊断已提交，日志待补传”，请点击“补传诊断日志”。把最终显示的报告编号／日志编号，以及版本和构建号发回来即可。
>
> 如果已经进入主界面，可以在“设置 → 上报错误日志”上传。请告知你使用的是 Apple 芯片还是 Intel，以及系统版本。

通过现有的受控文件分享渠道传送 DMG，同时提供版本、架构和 SHA256；不需要为单次诊断发布到官网。本次没有替你发送任何文件或客户消息。

## 验收边界

源码测试包含：错误源回退、校验失败换源、安全续传、Range 不一致、416、完整缓存复用、下载预算、错误分类、托管身份与模型就绪、预热单次执行及退避、部分上报失败后重载补传、悬浮咨询输入保留与请求拦截、原生上传限制。

最终 DMG 在客户机器上的安装、首次启动和真实 OSS 上传，必须以新包实测和收到的日志回执验收，不能用源码测试替代。

## 本轮验证结果

- Python 3.9.25：下载、预热、初始化、冷启动隔离沙箱、托管身份共 57 项通过。新增和修改的 5 个运行时 Python 文件通过 Python 3.9 语法及联合类型注解检查。
- Rust 桌面壳：9 项通过，1 项需真实 OCR 环境的既有测试保持忽略。原生上传增加非法 URL、空归档和非法请求头的阻断用例。
- 前端：本次相关测试通过；全量最后一轮为 682 项通过、4 项失败。失败均在 `SettingsDebugMode.test.tsx`，旧断言要求总请求数为 2，但当前工作区新增的 PermissionPreparation 组件多发出一次权限查询。未为本次任务改动这部分无关设置行为或断言。
- TypeScript / Vite 生产构建通过；已有的大 chunk 提示仍存在。`git diff --check` 通过。
- 本地浏览器打开真实悬浮咨询，确认正常就绪时输入可编辑、填入文字后发送按钮启用；没有提交测试咨询。故障状态及补传流程以自动化测试验证，未向生产服务上传本机日志。
- helper 自检命令在 Python 3.9 环境通过；最终包校验已接入构建脚本，但本轮未构建 DMG，故不声称最终 DMG 或客户机器验收通过。
