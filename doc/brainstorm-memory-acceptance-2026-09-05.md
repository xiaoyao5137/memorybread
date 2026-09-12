# 脑暴记忆接入验收

2026-09-05，源码实现与回归验收完成。范围是创作前置脑暴，包括首题、每轮分支探索、继续方向、恢复简报以及正文创作消费；没有执行 DMG 打包或安装包替换。

## 最终行为

- 每轮根据原始需求检索本地记忆；有当前分支、简报或回答时，再补充该分支检索。两路交错选择、去重，最多 6 个来源、12,000 字正文；长文保留命中窗口。已有引用段落和排除约束不用于自我强化查询。
- 独立核对尚未由用户决定的维度。明确、仍适用且无冲突的历史决定，经来源 ID 和原文校验后计入覆盖，不从头重复询问。当前用户要求最后呈现且优先于历史记忆；无可靠核对结果时不自动跳过。
- 思路可以选择来源 ID，由代码附上来源标题、日期、定位编号和真实摘录；模型显式提交的引文必须逐字存在于本轮来源。未知 ID、伪造引文会触发修正重试。未获直接支持的思路标为待验证推演。
- 命中、未命中、部分失败和失败分别提示。失败日志只记录异常类型，不记录资料正文或异常私有内容。
- Core 在原有会话 JSON 中保存 memory_brief，合入“历史记忆参考（当前用户修订优先）”段落。旧会话默认空，恢复保留引用，新轮次刷新引用。正文生成能消费这个参考段落；它不冒充用户确认答案。
- 前端沿用现有问题说明、选项说明和简报呈现，兼容原接口消费者。模型漏写结构字段但明确引用有效 mN 时可恢复真实来源；用于引用的内部字段不会直接显示在选项说明中。

## 自动化验收

| 范围 | 结果 | 关键覆盖 |
| --- | --- | --- |
| Python 脑暴、记忆、正文 Agent、队列与检索回归 | 273 通过，1 个 opt-in live 用例在常规运行中跳过 | 首轮/分支检索、真实 SQLite 到提示词、承接目标、引用校验、降级、长文、预算、正文消费 |
| Core 创作相关单测和 API 集成测试 | 65 通过 | 旧会话兼容、记忆简报保存/恢复/刷新、修订与分支状态机 |
| 前端脑暴、简报编辑与布局 | 31 通过 | 记忆来源和未验证说明实际渲染，现有交互回归 |
| 独立真实本地模型验收 | 1 通过，44.13 秒 | 真实 SQLite 检索 + 本机 qwen3.5:4b 生成；承接旧目标后转向使用者流程；用户取消旧目标后不再继承 |
| Python 3.9.25 | 通过 | 三个本次改动 Python 文件的 3.9 解析、编译及类型注解不含 3.10 联合语法 |
| git diff --check | 通过 | 无新增空白错误 |

真实模型使用隔离 SQLite 中的合成项目记忆，没有修改用户记忆库。该用例可显式重跑；正常回归不依赖下载模型或本机推理服务。Core 的一个既有技能分析时限测试在并发回归中超时，按单线程完整重跑上述创作范围后全部通过。

## 复现命令

在 `ai-sidecar` 下执行：

```sh
.venv/bin/python -m pytest -q tests/test_brainstorm_memory.py tests/test_creation_brainstorm.py tests/test_creation_references.py tests/test_creation_agent_loop.py tests/test_creation_queue.py
RUN_LIVE_BRAINSTORM_MEMORY=1 .venv/bin/python -m pytest -q tests/test_brainstorm_memory.py -k live_local
```

真实模型可通过 `BRAINSTORM_OLLAMA_URL`、`BRAINSTORM_LOCAL_MODEL` 覆盖本机默认配置。

在 `core-engine` 下执行：

```sh
$HOME/.cargo/bin/cargo test creation --lib --test api_tests -- --test-threads=1
```

在 `desktop-ui` 下执行：

```sh
npm test -- --run src/__tests__/CreationPanelBrainstorm.test.tsx src/__tests__/CreationBriefEditor.test.tsx src/__tests__/CreationPanelLayout.test.ts
```

实现入口：`ai-sidecar/creation/brainstorm.py`、`core-engine/src/api/handlers/creation.rs`。内部兼容契约见 `shared/creation-brainstorm/README.md`。新增检索和事实核对会增加有记忆命中时的每轮生成耗时；本次真实验收覆盖代表性合成场景，不代表对用户全部历史资料的质量评估。
