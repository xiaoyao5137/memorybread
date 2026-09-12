# 复测说明

本目录保留可阅读的结果、断言和输出正文。原始失败记录、完整模型回复及任务开始时的文件基线位于 `/tmp/mb-creation-qa-20260908/`。修复前失败不会通过修改报告标记变成通过；局部复跑结果注明原始记录路径。

## 自动回归

在 `MemoryBread/ai-sidecar` 中运行：

```sh
PYTHONPATH=. .venv/bin/python -m pytest -q -k 'creation or brainstorm'
```

在 `MemoryBread/desktop-ui` 中运行：

```sh
npm test -- --reporter=dot Creation creation
npm run build
```

Core 按筛选组记录：[创作单测 68 项](core-unit.log)、[创作 API 17 项](core-api.log)、[脑暴 API 18 项](core-brainstorm-api.log)、[创作操作与断点重试集成 4 项](core-durable.log)、[空简报兼容 1 项](core-checkpoint.log)，各组可能重叠，不能相加作为不重复总数。新增简报恢复用例之前的[原子操作 3 项快照](core-operations.log)仅作历史记录。[Core release 构建](core-build.log)、[前端创作 247 项](frontend-creation.log)和[前端构建](frontend-build.log)另存日志。Python 创作回归见 [python-creation.log](python-creation.log)，Python 3.9 检查文件清单见 [python39.log](python39.log)。本次未执行 DMG 打包。

在 `MemoryBread/core-engine` 中可按下列命令复现对应筛选组。这些是依据日志的运行目标与测试定义核对的等效命令，日志没有保留原始 shell 命令文本。创作 API 的 17 项属于新增三项资料权限测试之前的快照；当前同一 `creation` 筛选会覆盖 20 项，新增三项已包含在后续脑暴 API 的 18 项通过记录中。

```sh
cargo test --lib creation
cargo test --test api_tests creation
cargo test --test api_tests creation_brainstorm
cargo test --test creation_operation_tests
cargo test --lib api::handlers::creation::tests::creation_checkpoint_requires_unchanged_explicit_source_inputs -- --exact
cargo build --release
```

## 真实本地模型

在 `MemoryBread/ai-sidecar` 中逐条执行。以下脚本使用合成输入；检索脚本建立临时数据库并实际调用生产检索。不要并行运行这些模型评测，以免共享本地模型的排队耗尽单次超时。

```sh
PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_intent.py --output /tmp/creation-intent-recheck.json
PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_user_paths.py --output /tmp/creation-user-paths-recheck.json --timeout 1200
PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_delivery.py --review-only --output /tmp/creation-delivery-recheck.json
PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_memory_path.py --output /tmp/creation-memory-recheck.json --timeout 1200
```

意图、用户路径和验收脚本可用 `--case` 选择已定义的单个样本；未知样本不得当成通过。意图脚本的 `--list` 仅列出输入，不调用模型。`prompt_override` 为空的报告才代表正式生产分类提示；带覆盖提示的实验记录不算正式通过证据。

原始混合指令及六项来源对照可分别选择以下样本（使用新的输出文件名，原始调用记录按追加方式保存，不能把不同运行的 trace 行数相加当成一次运行）：

```sh
PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_user_paths.py --case mixed-question-writing --output /tmp/creation-mixed-recheck.json --timeout 1200
PYTHONPATH=. .venv/bin/python scripts/evaluate_creation_delivery.py --review-only --case reject-unprovided-audience --case accept-explicit-audience-option --case accept-authorized-fictional-relationships --case reject-unprovided-event-process --case accept-supplied-event-process --case accept-neutral-meeting-invitation --output /tmp/creation-source-scope-recheck.json
```

模型判断需要与正文人工复核结合：核对数字和所属字段、日期依据、否定句、表格行列关系、只读回答不改正文、局部编辑保持范围外原文，以及成稿是否自然完整。结构化返回成功或关键词出现不等于内容已经验收。

最终六项来源控制由五个 v23 真实通过记录和受影响事件负例的 v24 真实复测组成；不是六项统一在 v24 重跑。[请求回放](review-protocol-replay.json)逐条比较当前代码生成的提示、结构约束、预算及公开结果，确认五项已通过调用没有改变；原混合指令的两次审查与重新成文输入也保持一致。[最终来源对照](source-scope-controls.json)保留各项实际版本、耗时和完整回复，以及内部解释或定位仍不准确的证据边界。

## 页面验收

`ui-acceptance.json` 记录真实页面生成通知、精确替换地点、只读追问、刷新恢复和新建会话的验证。记录使用专用合成会话，保留在创作历史中；未覆盖既有用户记录。页面操作与后端持久化正文同时核验。

`ui-brainstorm.json` 记录读书交流会脑暴的五个中性维度、资料限制、多选分支、手动修改、终止后刷新恢复。最初不合格的专用会话保留为失败证据；最终通过使用另一条新建会话。页面显示和持久化状态证明本次采用限制资料的路径，分析与检索调用前的拦截由对应代码和传输回归共同验证，并非网络抓包。

`source-manifest.json` 记录交付源码的 SHA-256；`task-changes.patch` 和 `change-statistics.json` 按任务开始时保存的文件基线生成，避免把仓库原有未提交修改算成本次修复。原本未跟踪的 `CreationBriefEditor.tsx` 使用当时另存的原始组件副本，只有 11 行增加、5 行删除，不能按整个新文件统计。每个文件的基线来源及哈希保存在统计记录中。真实模型记录属于逐项修复后的代表样本；最终自动回归覆盖后续变更，不能据此声称所有历史样本均在最后一次代码变更后重新生成。

[源码与补丁核对](artifact-verification.json)记录 32 个任务路径（22 个 Python、4 个前端、4 个 Core、2 个真实审查回放 JSON）、22 个 Python 检查哈希及只读反向补丁检查结果。`git apply --check --reverse` 仅验证补丁可对应当前任务文件，没有实际应用或回退任何修改。[Python 检查时的源码哈希](python-source-hashes.log)可与源码清单交叉核对。[完整生成修正复核](repair-review.md)区分独立调用诊断、真实生产修正入口与完整混合指令，原始失败继续见[失败记录](mixed-failure-review.md)。
