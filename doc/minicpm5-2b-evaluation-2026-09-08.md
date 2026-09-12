# MiniCPM5-2B 与现用 Qwen3.5-4B：调研和 A/B 测试评估

日期：2026-09-08（Asia/Shanghai）。读者：MemoryBread 产品与研发。范围：公开资料核验、本机只读基线审计、测试与切换建议。本轮未下载候选权重、未启动推理测试、未修改产品配置或运行时代码。

## 结论与决策

建议将 **MiniCPM5-2B 最终发布版的 GGUF Q4_K_M** 纳入隔离 A/B 测试，目前继续使用 Qwen。候选下载包明显更小，公开工具调用与部分推理成绩有优势；中文知识提炼、材料忠实度、短预算输出质量和实际运行峰值尚未验证。Q8_0 可作为 Q4 质量不足时的第二候选。[S1][S2][S3]

这次要回答的是：能否在 MemoryBread 的真实任务、等待时间与内存限制下，用更小模型维持或提高业务完成质量。公开综合榜小幅领先只能支持启动测试，不能替代切换验收。

用户提供的微信文章链接返回验证页，浏览器访问也受限，未能读取文章标题、作者、日期和正文。因此下文不将任何数值归因于微信原文，使用官方模型卡、官方部署资料、Artificial Analysis（AA）及本地实况核验。[S12]

## 模型尺寸与能力范围

| 维度 | MiniCPM5-2B | 当前 Qwen3.5-4B |
| --- | --- | --- |
| 总参数 | 2,516,756,480，约 2.52B | 4,659,865,088，约 4.66B，含视觉权重 |
| 名称口径 | 非嵌入参数约 1.98B | 官方称语言模型 4B；完整模型较大 |
| 架构 | 稠密 Llama；42 层 GQA | 稠密混合架构；24 层 Gated DeltaNet + 8 层全注意力 |
| 输入 | 纯文本 | 文本、图像、视频能力 |
| 原生上下文 | 131,072 tokens | 262,144 tokens；本机当前加载 32,768 |
| 16 位权重规模 | BF16 参数理论约 5.03 GB；官方 F16 GGUF 5.04 GB | 按总参数 × 2 字节约 9.32 GB，非当前量化包大小 |
| Q4_K_M 文件 | **1.561 GB / 1.454 GiB** | **3.390 GB / 3.157 GiB**，本机实际 blob |
| Q8_0 文件 | 2.680 GB / 2.496 GiB | 当前部署不是 Q8，本轮不作为基线 |
| 许可 | Apache-2.0 | Apache-2.0 |

来源：[S1][S2][S4][S5][S7]。GB = 10^9 字节，GiB = 2^30 字节。完整参数统计含嵌入/视觉等差异，不能当作同等任务计算量之比。Qwen 的 4B 子型号不是 MoE，不能套用系列总介绍中的 MoE 特征。

Mini Q4 文件为 **1,561,318,368 bytes**，现用 Qwen 模型 blob 为 **3,389,971,840 bytes**，前者小 **53.94%**。Mini Q8 文件为 **2,679,710,688 bytes**，仍比现用 Qwen 文件小约 **20.95%**。这是存储/下载优势，不是运行内存或速度收益。[S2][S7]

本地模型目录元数据 `model_manager.py:96` 仍写 `size_gb=2.3`，与当前文件不符；本报告采用实查大小。当前 Ollama `/api/tags` 含小型配置层的合计大小为 3,389,983,735 bytes。[S7]

## 内存：已测基线与候选估算

本机为 Apple M4 Pro、48 GiB 统一内存，托管 Ollama 0.30.8，激活 `mbem-v1-local` → `qwen3.5:4b`，GGUF / Q4_K_M。[S7]

| 口径 | Qwen 已有证据 | MiniCPM5-2B |
| --- | --- | --- |
| Ollama 报告加载内存 | 2026-09-08 13:18:59 CST，32K ctx：**4.010 GiB** | 未实测 |
| 服务进程 RSS | 两次快照约 5.56、6.69 GiB；有活跃工作负载 | 未实测 |
| 统一 physical footprint 的生成峰值 | 2026-09-05 隔离历史实验，16K ctx：**4.87 GiB**；完成后驻留 4.78 GiB | 未实测 |

当前 `/api/ps` 的 `size` 和 `size_vram` 均为 4,305,893,456 bytes。它与 RSS、进程物理占用、整机内存变化是不同口径，不能相加。两次 RSS 快照不是完整峰值采样；9 月 5 日结果是历史实测，本次仅复核归档，未重新执行。[S7][S8]

根据官方 Mini 配置，假设单序列、未量化 FP16 KV 缓存，理论缓存为：

`42 层 × 2（K/V）× 2 KV 头 × 128 head_dim × 2 字节 = 43,008 字节/token = 42 KiB/token`。[S5]

| 上下文容量 | Mini 理论 KV 缓存 | 加上 Q4 文件体积后的预算参考 |
| --- | ---: | ---: |
| 8K | 0.328 GiB | 1.782 GiB |
| 16K | 0.656 GiB | 2.110 GiB |
| 32K | 1.313 GiB | 2.767 GiB |
| 128K | 5.250 GiB | 6.704 GiB |

最后一列只是权重文件体积与缓存的算术估算，**不含运行时、计算缓冲、临时激活、分配器等，不能标成实测 RAM 或显存需求**。实际值还取决于缓存精度、分配策略、批处理和 Metal 实现。Qwen 的八层全注意力理论 FP16 KV 为 32 KiB/token，另有循环状态；Mini 参数少，但 KV 随上下文增长的斜率并不更低。[S5]

因此，短至中等上下文值得验证内存收益；不能宣称总内存减半，也不能把 128K 上下文当作低内存默认配置。第一轮优先比较相同 16K、32K 配置。

## 榜单与质量

AA 榜单近期改版，必须保持版本与模式一致：

| AA Intelligence Index 口径 | MiniCPM5-2B | Qwen3.5-4B Reasoning |
| --- | ---: | ---: |
| 9 月 7 日发布文章，v4.2 | 15 | 14，estimated |
| 9 月 8 日直接读取模型页，v4.3 | 14，estimated | 13，estimated |

来源：[S3][S6]。Mini 在更早的 v4.1.1 为 23；23、15、14 的变化不能解释成模型质量变化。v4.3 页含估计标记，不能写成两款均已完成该版独立实测。这里比较 reasoning 版本，不代表本地关闭思考、Q4 量化的分数。

以下为 **OpenBMB 同一张模型卡对照表的摘录**，均为分数越高越好；不混入 Qwen 自家不同设置的数据。[S1]

| 评测 | Mini | Qwen | 较高者 |
| --- | ---: | ---: | --- |
| 官方所列项目平均 | 53.9 | 51.1 | Mini |
| LiveCodeBench v6 | 69.1 | 56.4 | Mini |
| AIME 2025 | 86.5 | 78.8 | Mini |
| BFCL v4 | 66.6 | 56.8 | Mini |
| BrowseComp-ZH | 43.5 | 39.6 | Mini |
| IFBench | 66.3 | 59.0 | Mini |
| IFEval | 86.7 | 90.2 | Qwen |
| MMLU-Pro | 70.8 | 78.0 | Qwen |
| GPQA-Diamond † | 70.2 | 77.1 | Qwen |
| AA-LCR † | 59.0 | 61.0 | Qwen |
| NoLiMa | 68.1 | 43.5 | Mini |
| LongBench v2 | 43.7 | 47.3 | Qwen |
| SWE-bench Verified | 46.4 | 33.6 | Mini |
| SWE-bench Pro | 14.4 | 28.2 | Qwen |
| Terminal-Bench v2.1 † | 8.6 | 25.8 | Qwen |

† 原表注明来自 AA；其余为 OpenBMB 内部复现。公开卡未逐项完整披露统一的量化、输出预算和采样配置，不能称为全部条件一致的本地对照。53.9 是所选项目平均，不是行业统一质量百分比。[S1]

对 MemoryBread 的含义是推断：工具调用与检索规划值得试；通识、长材料理解、精细指令执行仍要防退化。BFCL 较高不能替代 JSON schema、中文引用和字段事实准确率验收；SWE-bench Verified 较高也不能概括为所有代码 Agent 场景都更强。

还要注意两个质量与体验边界。AA 发布文章记录 Mini 每题平均约 **19K 输出 tokens，其中 11K 为思考**；这不是 MemoryBread 普通查询的预期长度，但说明榜单质量依赖的推理预算与短交互不同。AA-Omniscience 的 78% non-hallucination 指标伴随仅 29% 作答率、8% 准确率，不能解释成知识准确率 78%，也不等于引用材料可靠率。[S3]

AA 方法说明主要覆盖英文文本，reasoning 模式按模型允许的输出预算运行；不能代替中文提炼、RAG 和 Brainstorm 实测。当前没有本机 Mini 的可信速度数据；不把远端供应商吞吐当成本机吞吐。[S9]

## MemoryBread 接入成本

1. **GGUF 路线可行，当前版本兼容仍需跑通。** 官方提供标准 Llama GGUF 和 Ollama/llama.cpp 部署文档，可先试现有 0.30.8。官方关于 Jinja 转 Go 模板的版本提示针对 0.24，不能直接当作 0.30.8 的实测结论。需要检查模型模板、EOS、流式、停止条件和 Metal 加载。[S10]
2. **思考控制是主要适配点。** 提炼默认 32K ctx、8K 输出，咨询默认 1K 输出；这些路径禁用 thinking。创作快速结构化操作同样禁用，Skill 路由则保留思考。部分现有路径仅为 Qwen 构造 raw 模板；创作非 Qwen 分支没有等价传递 `disable_thinking`。只改模型名可能导致长思考、空正文或超时。[S7]
3. **需要验证候选自己的模板和输出契约。** Mini 官方 Jinja 含 `enable_thinking=False` 分支，但没有找到 2B 独立 no-think 质量表，不能移用 1B 模型数据。其原生函数调用为 XML 风格，官方推荐 SGLang parser 转为标准 `tool_calls`；Ollama 能生成文本不等于这些工具契约已经适配。[S1][S11]
4. **当前 OCR 文本路径可先测。** 咨询 prompt 明确图片只含 OCR 转录、不含像素，创作截图核验也使用 OCR。Mini 纯文本因此不是当前这些流程的自动否决项；如果使用 Qwen 原生像素理解，则需要单独保留或替代视觉模型。MiniCPM5-2B 与项目旧独立 MiniCPM-V 不是同一个模型。[S7]
5. **正式切换涉及多个入口。** 除模型注册外，初始化固定捕获模型、Rust 咨询/创作品牌映射也引用 Qwen，应按统一模型能力配置治理，避免只改一个文件。仅替换生成模型且保持 embedding 时，不需要因模型名变化重建全部向量索引；新提炼内容质量和写入契约仍要回归。[S7]

上述是接入审计与实施建议，本轮未修改这些代码。

## 建议的 A/B 测试规范

先完成约 10 个冒烟案例，再用 50–100 个固定业务样本筛选；关键案例与失败案例用至少 3 次独立生成检查稳定性，通过后扩大至几百条真实脱敏回放。少量无失败样本不代表统计上已证实稳定。

| 阶段 | 具体做法 | 决策用途 |
| --- | --- | --- |
| 环境 | 独立实验目录、模型存储、空闲端口；保留正式 Qwen、配置和运行时；同机依次执行，控制后台排队 | 确认性能差异来自候选配置 |
| 冒烟 | Mini Q4、当前 0.30.8；加载、首轮/多轮、stream、JSON schema、EOS、关闭/开启思考、取消/抢占、16K/32K | 能否承接现有调用契约 |
| 产品预算组 | 同一原始材料、OCR、召回结果、业务 prompt、输出上限与超时；各模型使用正确模板及可比模式 | 是否在当前用户等待时间内提升完成质量 |
| 能力组 | 分开使用各模型推荐参数与 thinking；记录全部思考 tokens 和耗时 | 判断能力上限及其代价，不与产品预算组混算 |
| 质量候选 | Q4 不足时再测 Mini Q8_0，必要时以 F16 作少量诊断 | 区分量化损失与模型/模板问题 |
| 内存/性能 | 16K、32K 下分别测冷/热启动、首个可见正文、完成耗时、重试、截断、取消、吞吐；采样进程树 physical footprint/RSS、Ollama ps、swap、系统内存压力 | 判断常驻、峰值和尾延迟收益 |

固定源文本，并同时记录两种 tokenizer 的实际输入/输出 token 数；不能把不同 tokenizer 下的 token/s 比值直接当作中文阅读速度提升。队列等待、模型加载、prompt 处理、思考和正文生成耗时分别统计。初轮用副本或临时数据库，保留原始会话、输入与已有知识。[S7][S8]

测试样本应覆盖以下并列场景：

- **RAG 咨询**：相关/无关记忆混合、无证据问题、时间冲突、数字与单位、[M#] 采用和引用、OCR 图表只有标签而无读数；观察错误引用和无依据扩写。
- **知识提炼**：事实保真、主题颗粒度、标题、重复知识、JSON schema、必需字段、材料不足时的输出；不只检查 JSON 可解析。
- **Brainstorm/创作控制**：首次问题耗时、多选分支、选项 ID、证据原句、简报编辑、Skill 路由、精确追加/替换；不让模型替用户选择方向。
- **中文长文**：完整交付、逻辑连贯、遗漏约束、事实与推断边界、重复段落，包含短材料和接近实际 16K/32K 使用量的材料。

先冻结 OCR 和召回结果做纯模型对照，再运行完整业务链；检索改写、确定性材料总结和代码校验可能影响最终表现，需要从结果中区分。可复用已有 `doc/evaluations/2026-09-05/` 性能/内存脚本；`ai-sidecar/scripts/evaluate_creation_memory_path.py` 支持 URL/模型注入、临时 SQLite 与真实模型调用，可作为创作 A/B 的现成入口。[S7][S8]

建议在测试前固定以下切换门槛，它们是本次建议，尚不是既有产品 SLO：

- 关键回归中新增严重事实错误、错误引用和破坏性编辑为 0；整体质量不能用综合榜分数抵扣。
- JSON/schema 首次通过率目标至少 99%，且不低于 Qwen；重试后成功与首次成功分开统计。
- 人工盲评中候选胜出或持平的样本占比目标至少 95%，报告样本数、分项结果及不确定性。
- 相同 ctx 下生成峰值 physical footprint 目标降低至少 20%，完成后常驻不增加；P95 首个可见正文和完成时间变慢不超过 10%。
- 流式、取消、恢复、初始化、模型路由和现有受影响回归通过；保留可回退的旧模型和版本标识。

全部通过后，再进入可回滚灰度。若仅工具规划明显收益，而事实类正文或交互延迟退步，可评估按任务路由，但双模型常驻会抵消内存优势，频繁换入又会增加延迟；不能默认同时加载两者。

## 局限与尚待确认

Mini 的本机峰值/常驻内存、速度、Q4 中文非思考质量、Ollama 0.30.8 实际模板行为、各业务闭环质量均未实测。公开权重和榜单发布很新，AA 页面已经换版；后续测试必须固定模型 revision、文件 SHA-256、运行时版本、模板和采样参数。

候选 GGUF 当前 revision：`f0c9de8f8e1bbffc5abf14e64ccb61bb99e4219d`；Q4 文件 SHA-256：`ec2d5801640099e97d8d7e8003ad4d81f336e757811f03a26173dddf386602fd`。Mini 原始权重 revision：`0e9c66dce9fedde5ba8663bbcdd54b6810bb929a`。Qwen 官方 revision：`851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a`。当前本地 Qwen manifest digest：`2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`。这些是本次读取的元数据，候选大文件未下载，也未在本地重算其 hash。[S2][S4][S7]

## 来源登记

外部来源均于 2026-09-08 访问；除已注明日期的发布文章外，不将搜索索引日期当作发布日期。

- **S1**：[OpenBMB MiniCPM5-2B 模型卡](https://huggingface.co/openbmb/MiniCPM5-2B)。原始发布方；参数、能力、同表评测及限制，非本地 Q4 验收报告。
- **S2**：[官方 GGUF 文件列表](https://huggingface.co/openbmb/MiniCPM5-2B-GGUF/tree/main)、[文件 API](https://huggingface.co/api/models/openbmb/MiniCPM5-2B-GGUF/tree/main)、[仓库 revision API](https://huggingface.co/api/models/openbmb/MiniCPM5-2B-GGUF)。只读取元数据和小型文档，未下载权重。
- **S3**：[AA MiniCPM5-2B 发布分析](https://artificialanalysis.ai/articles/openbmb-releases-minicpm5-2b)，2026-09-07。独立评测机构自己的报告，固定 v4.2 发布口径及输出预算数据。
- **S4**：[Qwen3.5-4B 官方卡](https://huggingface.co/Qwen/Qwen3.5-4B)、[Qwen 参数/revision API](https://huggingface.co/api/models/Qwen/Qwen3.5-4B)、[Mini 参数/revision API](https://huggingface.co/api/models/openbmb/MiniCPM5-2B)。完整 safetensors 参数统计用于纠正模型名称口径。
- **S5**：[Mini 官方 config](https://huggingface.co/openbmb/MiniCPM5-2B/blob/main/config.json)、[Qwen 官方 config](https://huggingface.co/Qwen/Qwen3.5-4B/blob/main/config.json)。KV 为本报告据结构推算，不是官方内存实测。
- **S6**：[AA Mini 当前页](https://artificialanalysis.ai/models/minicpm5-2b)、[AA Qwen Reasoning 当前页](https://artificialanalysis.ai/models/qwen3-5-4b)。访问时 v4.3，并保留 estimated 限制；内容可能继续更新。
- **S7**：本机只读 API `/api/version`、`/api/show`、`/api/tags`、`/api/ps` 与模型 manifest；[模型注册](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/model_manager.py:90)、[运行时版本](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/initialization_manager.py:40)、[提炼参数](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/knowledge/extractor_v2.py:30)、[提炼模板](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/knowledge/extractor_v2.py:3120)、[咨询调用](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/rag/llm/ollama.py:79)、[咨询预算](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/rag_api_server.py:57)、[创作调用](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/creation/service.py:4559)、[OCR 边界](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/model_api_server.py:594)、[创作映射](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/core-engine/src/api/handlers/creation.rs:4042)、[咨询映射](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/core-engine/src/api/handlers/query.rs:194)、[隔离创作回归入口](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/ai-sidecar/scripts/evaluate_creation_memory_path.py:292)。运行数据是本次快照，代码是未提交工作区现状。
- **S8**：[2026-09-05 本地推理实验归档](/Users/xianjiaqi/Documents/mygit/mb-all/MemoryBread/doc/local-inference-evaluation-2026-09-05.md:63)。历史实验，本次重新读取；不当作本次复测。
- **S9**：[AA 评测方法](https://artificialanalysis.ai/methodology/intelligence-benchmarking)。评测机构的方法说明，用于限制语言、模式、预算的可比性。
- **S10**：[OpenBMB Ollama 部署文档](https://github.com/OpenBMB/MiniCPM/blob/main/docs/deployment/ollama.md)、[llama.cpp 部署文档](https://github.com/OpenBMB/MiniCPM/blob/main/docs/deployment/llama_cpp.md)。官方部署可行性说明，不能替代项目当前版本验收。
- **S11**：[Mini 官方 chat template](https://huggingface.co/openbmb/MiniCPM5-2B/blob/main/chat_template.jinja)。思考与工具格式的代码来源，不等于 no-think 质量数据。
- **S12**：[用户提供的微信链接](https://mp.weixin.qq.com/s/QjIQhrayJK47SAN2E5TpdA)。正文访问受阻，仅登记用户输入，不作为事实证据。
