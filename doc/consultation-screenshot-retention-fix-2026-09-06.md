# 咨询历史截图清理修复

## 现场证据

- 本机数据库 `~/.memory-bread/memory-bread.db` 中，2026-08-30 的咨询记录 133–137 均通过 `retrieved_ids` 内的 `floating_assist` 上下文引用 `~/.memory-bread/floating-screenshots/*.jpg`。
- 五个被引用文件均不存在，截图目录为空。记录中的文字与参考资料仍然存在。
- 桌面端旧 `cleanup_floating_assist_temp_files` 对目录中所有超过 24 小时的文件执行删除，没有检查咨询记录引用。历史页读取失败后统一显示“截屏暂不可用”。

## 修复

- 清理前只读查询咨询历史，保留所有上下文中 `screenshot_path` 引用的文件，包括旧记录；不依赖场景字段或文件名判断。
- 只有超过原有保留时间且没有咨询引用的文件才可清理。引用消失后，过期文件仍可正常释放。
- 数据库不存在、不可读或引用 JSON 解析失败时，先返回错误，不执行删除，也不创建空数据库。
- 原图不存在时返回 `SCREENSHOT_NOT_FOUND`，历史页显示“原截图已丢失”；其他读取错误仍显示暂不可用。

## 验证与边界

- `npm test -- src/__tests__/RagPanelHistory.test.tsx src/__tests__/SystemFloatingAssist.test.tsx`：34 项通过。
- `cargo test --lib`：7 项通过；使用临时 SQLite 和真实文件验证跨保留期保护、未引用文件清理、取消引用后释放、数据库异常不删除。
- `npm run build`：通过版本检查、TypeScript 与 Vite 构建。
- `git diff --check`：通过。
- 开发客户端由既有 Tauri dev watcher 重新编译运行，核心服务保持运行。本次未更新 `/Applications` 中的安装版，未打包 DMG。
- 未完成新增真实屏幕咨询的端到端视觉验收。原有五张图片已经丢失，无法通过此修复恢复；未修改旧咨询记录。
