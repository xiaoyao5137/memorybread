# 咨询与创作推理优先级修复验收

本轮直接修复交互优先级、后台抢占和相关取消/错误边界；脑暴上下文及耗时展示以[待实施方案](interactive-latency-optimization-proposal-2026-09-08.md)交付。使用本地开发服务验证，不打包 DMG，不修改原脑暴会话的需求、回答或正文。

## 根因与修复

原队列已提供跨进程槽和抢占通知，但部分在线辅助入口没有经过队列。提炼的同步 HTTP 调用在等待响应头时尚未注册取消回调，读取期间跨线程关闭连接也可能阻塞。因此，只有 P0 标记和通知不足以让后台及时释放模型。

- 统一在线入口：咨询整轮、引用准备、创作/脑暴/局部编辑、Agent 子步骤，以及技能匹配、分析、审阅和模型体验调用均以 P0 运行；流式读取全过程保持同一队列身份，避免内部重复申请槽。
- 统一可取消模型传输：在发请求前注册取消，覆盖连接、等待响应头、输入处理和输出阶段；先关闭真实异步 HTTP 连接，等执行函数退出再释放槽。
- P0 注册与后台准入共用跨进程调度锁。P0 已排队时，P1/P2 不能抢占刚空出的槽；后台仍登记就绪，交互结束后恢复调度。
- 通过 `execution_origin` 显式区分在线与定时来源：在线默认为 P0，`scheduled_task` 使用 P2。被抢占的定时任务返回 `INFERENCE_PREEMPTED`，记为 deferred，不回退另一路模型，不计永久失败，也不保存/投递半成品；既有调度器按忙重试策略恢复。
- 取消尚未执行的 P0 时立即释放需求标记。新增创作辅助入口支持取消工作线程内实际异步请求，防止用户关闭请求后仍阻塞后续交互。
- 保留模型错误语义：日记/报告适配器遇到 HTTP 200 流内错误或缺少完成标记时拒绝返回成功。原 V2 专用错误类别和既有输出长度策略保持兼容。
- 启动脚本增加三个 Python 服务对共享传输模块的源码变化检测，补齐创作服务对队列模块的检测，确保后续重启加载修改。

共享规则见[调度契约](../shared/inference-priority/README.md)。当前产品启动与打包入口未使用的旧独立 `knowledge/api.py`、`rag/rag_server.py` 不计入线上覆盖；云端服务本身的可用性和性能不在本地模型资源验收范围。

## 入口覆盖

| 入口 | 调度身份 |
| --- | --- |
| `/query`、`/query/stream`、`/references` | 整轮 P0 |
| 创作 `generate`、`brainstorm/next`、本地 `inline-edit/run` | P0 |
| 创作 `skills/match`、`skills/analyze`、`skills/review`、`references`、`test_model` | P0 |
| 创作 `chat`、`/api/models/<id>/chat` | 流式读取期间 P0 |
| `agent/run` 在线与恢复、其模型子步骤 | P0 |
| 定时任务复用 `agent/run` | 显式来源 P2 |
| 时间线提炼、批量提炼/合并、日记/后台报告 | P1/P2，可被 P0 中断 |

当前咨询前置意图判断是规则回退，引用准备主要是 embedding/召回。本轮统一了入口身份，不把它们描述为已经存在的额外 LLM 改写调用。

## 验证与边界

最终测试和运行元数据保存在 [JSON 验收记录](evaluations/interactive-inference-priority-2026-09-08.json)。完整私有测试日志和合成探针在 `/tmp/mb-interactive-priority-20260908/`；发布资料不包含用户材料、模型原文或凭据。

结果：创作/咨询关联回归 1382 项通过；队列/传输/后台关联回归 409 项通过、1 项可选真实数据事实测试跳过（两组含重叠用例，不相加）。Rust 30 项通过，release 构建、Python 3.9.25 编译与注解检查、启动脚本回归及方案 strict audit 均通过。两组 Python 测试各有两项既有 Qdrant 版本探测提示。

| 最终真实模型样本 | 后台连接取消 | P0 取得调度槽 | P0 完整响应 |
| --- | --- | --- | --- |
| 输入处理阶段 | 47.54 ms | 107.51 ms | 2801.78 ms |
| 流式输出阶段 | 29.42 ms | 112.46 ms | 407.66 ms |

两次均确认模型服务的 `cancel task` / `stop processing`，之后高优请求成功，后台再次运行成功。另测创作聊天实际模型连接取消，后续 P0 在 113.26 ms 取得槽并完成响应。探针自身 Future 返回和 worker finally 清理有先后关系，需求锁在探针退出后另行核对已释放，不把即时采样的短暂占用误判成泄漏。

本地 Core、Sidecar、Model API 和 Creation 服务均已更新并核对进程启动时间晚于相关源码，健康检查通过；模型服务进程保持原 PID，验收没有依靠重启模型服务来释放资源。

测试分别验证队列身份、取消实际连接、连接清理后才能运行前台、跨进程准入、后台恢复、流内错误拒绝成功，以及用户取消后的连接清理。模拟 HTTP 和真实本地模型分别记录，不能相互替代。

真实本地模型使用生产机器级队列和独立合成请求，分别在首 token 前及输出期间提交 P0，并验证后台重获资源。取消连接耗时、队列准入耗时、模型完整响应耗时分开计算：取消连接不代表 GPU 内核可在同一毫秒停止，后端仍需处理取消并接收下一条请求。这些单次样本不构成延迟 SLA，也不代表长上下文已经压缩。

运行链路验收另包含本地咨询 SSE 完成、创作文档流完成及终态。首次咨询探针未显式选择本地，沿用了现有云配置并收到 HTTP 403；该尝试已保留为失败证据，显式选择本地后重跑成功，不计为云端验收通过。

原会话 `creation-1788839287733-8b87e3c942303` 的需求、简报、对话和正文与本轮前的恢复快照保持一致，正文仍为空；没有代用户回答脑暴问题。

复核命令：

```sh
# core-engine：Core 来源透传、持久化/恢复和脑暴传输
cargo test --test creation_model_boundary_tests --test creation_operation_tests --test brainstorm_transport_tests
cargo test --test api_tests brainstorm
cargo build --release

# MemoryBread 根目录：共享模块更新的启动检测
bash test/test-startup-freshness.sh
bash -n start.sh test/test-startup-freshness.sh

# MemoryBread 根目录：创作、咨询、入口与运行中取消
ai-sidecar/.venv/bin/python -m pytest ai-sidecar/tests/test_creation*.py ai-sidecar/tests/test_floating_assist.py ai-sidecar/tests/test_interactive_entrypoints.py ai-sidecar/tests/test_rag.py ai-sidecar/tests/test_consultation_material_retrieval.py ai-sidecar/tests/test_consultation_material_answering.py ai-sidecar/tests/test_memory_evidence.py -q

# ai-sidecar：队列、传输、提炼、日记和任务恢复
.venv/bin/python -m pytest tests/test_inference_transport.py tests/test_inference_queue.py tests/test_extractor_ollama_transport.py tests/test_bake_extractor.py tests/test_bake_api_timeout.py tests/test_data_fact_recovery.py tests/test_timeline_data_facts.py tests/test_scheduled_diary.py tests/test_scheduled_task_creation_executor.py tests/test_rag.py tests/test_consultation_material_answering.py -q
```

Python 改动同时用正式兼容基线 Python 3.9 做编译及注解语法检查。可选真实数据事实模型测试不作为本轮抢占回归的强制项；独立真实模型探针补充的是资源释放证据。
