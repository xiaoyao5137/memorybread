# 悬浮球本地 AI 状态误报与预热阻塞修复

## 根因证据

- 悬浮球状态请求在 8 秒后中止。修复前真实 `/api/initialization/readiness` 请求耗时 13.694 秒；`/status` 耗时 12.204 秒。
- 两个入口共用 `get_status()`；该方法持有初始化锁，反复执行 SQLite `PRAGMA quick_check`。本机单次独立扫描约 1.5–1.9 秒，多窗口轮询在同一锁上积压。
- Model API 日志显示 19:12 首次向量预热停在 0%。19:23 进程采样显示 PyTorch MPS index_select / GPU 内核编译栈；后台默认 CPU 的设计未落实到 SentenceTransformer 构造参数，库实际自动选择 MPS。

## 修复

- 日常状态只做数据库可访问性、必需表及迁移检查；初始化和修复保留完整性扫描及写入验证。
- SentenceTransformer 显式默认 CPU；支持构造参数和 `MEMORYBREAD_EMBEDDING_DEVICE` 覆盖，旧版本兼容重试仍保留设备选择。
- 检索能力预热返回 `LOCAL_AI_WARMING_UP`，连接失败返回 `LOCAL_AI_STATUS_UNAVAILABLE`。这些状态保留问题、禁止未就绪提交、提供重新检查，并自动轮询恢复；真实初始化失败仍有修复入口。

## 实测

- 当前开发客户端已自动重载修复。CPU 向量编码预热约 0.15 秒成功，RAG pipeline 完成初始化。
- 8 个并发 status/readiness 请求耗时 103–749 毫秒；所有 readiness 均为 true，所有初始化状态均为 completed。
- 在运行中的悬浮球页面打开咨询框、提交“启动验证：请只回答 2 加 3 等于多少。”，8.9 秒后显示“已生成”，回答为“5”；不再出现状态错误和修复提示。
- Python 3.9 执行相关后端测试 106 项通过；6 个修改的 Python 文件通过 3.9 语法与类型联合检查。TypeScript 类型检查通过。前端门禁与悬浮球回归包括预热/网络恢复/真实失败分支。

原生桌面正在运行并使用同一份前端代码；上述点击验收通过 localhost 的悬浮球页面完成，未重新打包安装版。
