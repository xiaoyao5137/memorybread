# 模拟初始化浏览器诊断上报修复

## 根因与现场证据

2026-09-06 在 `http://localhost:1420` 的模拟初始化失败页点击上报后，结构化诊断成功，但日志附件失败，报告编号为 `01a075ef-ccc9-7613-bbf7-c1816d3e38ea`。

使用不含用户内容的合成归档，对同一有效签名上传地址进行验证：

- OPTIONS，Origin 为本机开发页，预检方法为 PUT，请求头为 content-type：HTTP 403，OSS 返回 `AccessForbidden` 和 `CORSResponse: This CORS request is not allowed`。
- 服务端直接 PUT 同一签名地址和同一归档：HTTP 200。

因此是浏览器直传与对象存储 CORS 配置不兼容，不是上传签名失效。桌面 Tauri 原生上传已有独立通道，本轮未发现或推断其存在同样故障。

## 修复

- 浏览器开发模式通过同源 `/__memorybread/diagnostics/upload` 将归档交给本机 Vite 服务，再执行 HTTPS PUT。由开发服务器插件加载，后续正常启动开发服务自动生效。
- Tauri 仍优先使用原生命令，正式打包不会包含开发服务器转发插件。
- 转发仅接受本机 Host 与严格同源 Origin、POST JSON、签名 OSS 地址和 customer-logs 路径；限制请求头与 10 MiB 归档，禁止重定向，设置 55 秒超时，不返回签名地址或上游原始错误正文。
- 继续执行云端完成接口，只有收到日志回执才显示完整成功。补传复用原诊断编号。
- 开发服务器及测试用原生 JavaScript 模块隔离，避免向前端引入 Node 全局类型；未新增依赖。

## 验收

- 真实浏览器点击“补传诊断日志”，界面显示：`诊断与日志上报成功，编号 01a075ef · 日志 01a07602`。这意味着实际归档已传输，云端完成接口已返回有效日志编号。
- `customerLogReport.test.ts`、`dev/customerLogUpload.test.mjs`、`OnboardingWizard.test.tsx`：共 35 项通过，覆盖浏览器通道、原生优先、失败不误报成功、重载补传，以及转发地址、来源、头部、大小、重定向与错误信息边界。
- `npm run build` 通过，保留既有 chunk 大小提示；`git diff --check` 通过。
- 额外设置页回归 `SettingsDebugMode.test.tsx` 的 4 项既有失败仍在：全局 fetch 次数断言为 2，权限检查加入后为 3。此问题已在本仓 `initialization-diagnostics-and-remote-dmg-2026-09-06.md` 记录，本轮没有修改设置页行为或这些断言。
- 本轮未修改 Python、云端配置或桌面原生上传代码，未打包 DMG；不将浏览器验收等同于新 DMG 安装验收。

## 现场恢复

本轮注入前发现沙箱已变成 interrupted，因此先备份当时状态，再临时注入失败场景。验收后按字节恢复本轮备份，未覆盖其他操作造成的状态变化。备份位于 `~/.memory-bread/initialization-sandbox/state.before-report-fix-20260906.json`。上一次测试前的 completed 备份仍保留。
