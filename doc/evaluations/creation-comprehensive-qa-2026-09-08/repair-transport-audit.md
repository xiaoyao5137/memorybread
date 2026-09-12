# v13 修复调用传输核查

2026-09-08，只读核查；未运行模型、测试或修改生产源码。结论：未发现该次 system 或用户事实被客户端裁剪；可关联的运行日志也未显示上下文截断或输出额度耗尽。此结论不证明模型正确理解了内容。

证据及调用路径：

- 原记录：[delivery-repair-issue-chain-v13.calls.jsonl](/tmp/mb-creation-qa-20260908/delivery-repair-issue-chain-v13.calls.jsonl)，第 1 行、唯一记录，completed=true、num_predict=8192。recording_id=1788828328123275000，是 RecordingService 实例创建时间，换算为 2026-09-08 08:45:28.123275 +08:00；记录文件 mtime 为 08:46:15.219038 +08:00。
- 生成脚本：`/tmp/mb-creation-qa-20260908/replay-delivery-repair-chain.py:21-24`。本轮初读时第 21 行目标为 v13；归档时 owner 已将其改为 v14。第 22 行仍使用 RecordingService、qwen3.5:4b、http://127.0.0.1:11434；第 24 行使用 local 模式。该脚本是可变的复放入口，不当作不可变的 v13 源码快照。
- 记录包装：`/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/scripts/evaluate_creation_delivery.py:33-51`。先保存 system_prompt、user_prompt、json_schema、num_predict，再调用父类 `_stream_direct_completion`；记录的是 HTTP 组装前参数，output 是经过响应解析的文本。
- 客户端：`/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/creation/service.py:4351-4359` 将 JSON 修复设为 num_predict=8192、temperature=0、disable_thinking=true。`4485-4539` 仅在输出截断时有界增加输出预算并重新生成；v13 只有一条记录，记录额度仍为 8192。
- 实际 HTTP 组装在同文件 `4579-4628`：Qwen3.5 使用 POST /api/generate、raw=true、stream=true、top_p=0.9、repeat_penalty=1.0、presence_penalty=0.0。json_schema 放在独立 format 字段；未设置 num_ctx。loopback 使用 trust_env=false，直接 httpx 发送。
- 同文件 `9242-9254` 的 `_build_qwen35_prompt` 仅完整拼接 system/user、Qwen 模板及关闭思考前缀；该入口到 HTTP 发送之间无字符串截取或消息裁剪。`9256` 起的解析只处理响应、过滤 thinking，不裁剪输入。

v13 记录中 system 为 1,984 字符，user 为 5,789 字符；按已读模板重建 raw prompt 为 7,872 字符、14,080 UTF-8 字节，包含原用户事实及不检索/不补充约束。format schema 序列化为 8,366 UTF-8 字节，未拼进 raw prompt。上述长度是记录与模板重建值，不是 wire 抓包测量值，也不是 token 估算。

运行时关联证据：

- 只读 lsof/ps 显示 11434 由 Ollama PID 83314 监听，进程自 2026-09-07 18:10:41 启动；可执行文件为 `/Users/xianjiaqi/.memory-bread/initialization/runtime/ollama/v0.30.8/runtime/ollama serve`。GET /api/version 返回 0.30.8；GET /api/ps 当时返回 qwen3.5:4b、context_length=32768。这些 GET 没有触发生成。
- 原日志 `/Users/xianjiaqi/.memory-bread/logs/ollama.log:180821`：task 783466，n_ctx_slot=32768，task.n_tokens=3350；`180856-180858`：prompt eval 3350 tokens，输出 607 tokens；`180860`：truncated=0；`180862`：2026/09/08 08:46:15，POST /api/generate，HTTP 200，耗时 46.879933416s。该完成时间和耗时与记录时间强关联。仅该 task 的推理时间约 17.07s，POST 耗时包含等待，不将两者混用。
- 已复制 18 行原始元数据到 [repair-transport-audit.log](repair-transport-audit.log)，每行带原路径及行号；没有复制提示词或无关用户内容。

证据限制：记录未包含完整 wire 请求、wire hash、服务端 request_id 或原始终帧 usage。task 783466 与 v13 是按时间、耗时和接口关联，并非请求 ID 或内容 hash 证明；因此不能宣称已逐字核验模型接收到的输入。可确认的是：已读客户端路径无裁剪、实际关联 slot 为 32,768、该 task 输入 3,350/输出 607 token 且 truncated=0。没有证据将 v13 的选择错误归因于 system/事实静默丢失或 8,192 输出额度耗尽。
