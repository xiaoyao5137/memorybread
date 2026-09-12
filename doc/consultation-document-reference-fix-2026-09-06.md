# 咨询文档排序与悬浮球引用跳转修复

## 现场

本机 rag_sessions 145 的用户问题为“SMACT的文档”。第一条外部引用是“H3模型适配 Meta prompt - 云文档”，capture_id=73137，没有 document_id/artifact_id，正文只有标题与 JavaScript 启用提示。其融合分数为 1/61。模型 API 日志显示知识库候选 60 条、待烘焙通道 0 条，最终选中 8 个融合候选与 2 个补位候选。

修复前真实 /references 重放同一问题，H3 再次进入前三，前排同时出现无关视频提示词文档。说明它不是 SMACT 的最佳证据，而是语义通道的误召回。现有融合以通道名次计分，没有按明确的文档主题校验候选；噪音规则也未拒绝 JavaScript 页面空壳。

悬浮球虽携带 source_type=document，却没有内部文档 ID。点击事件未传原文 URL，App 无条件进入文档列表、将目标 ID 设为空，故未打开原文。

## 修改

- 对明确指定产物类型且存在区分性主题词的查询，沿用语料感知查询规划器，在融合前校验各通道候选正文、标题、摘要是否有主题词依据；英文忽略大小写并按词边界匹配。未按具体模型或 SMACT 写死规则。
- 排除短小 JavaScript 启动空壳，保留有实质正文、仅讨论该错误的资料。
- 悬浮球与咨询页传递原文 URL。内部文档 ID 存在时沿用详情打开；没有 ID 时打开 HTTPS 原文，兼容旧记录 document_url 键；无 URL 或打开失败时使用采集记录兜底。

## 验证

- Python RAG 与 floating_assist 回归：114 passed。
- App、悬浮球、咨询历史前端回归：71 passed。覆盖 URL 传递、历史 URL 键、待烘焙引用、无 URL 的采集兜底和内部 ID 原有路径。
- TypeScript/Vite 构建、Python 3.9.25 py_compile、git diff --check 通过。
- 重新加载开发 Model API 后，真实 /references 重放得到 7 条均含 SMACT 的资料，首位《容器云 GPU 指标采集项目》（document_id=80），第二位《电商 GPU 使用情况一览 (2026-07-21)》（366）。H3 与无关视频提示词文档被排除。
- 重放使用 references-only，无生成模型调用、不写咨询历史；原始 145 保留。原始/新结果保存在本机 /tmp/mb-rag-145/before.json 和 after.json。
- 跳转由组件与路由回归验证；电脑控制工具未识别正在运行的开发版窗口，因此未宣称完成原生悬浮球到浏览器原文的端到端视觉验收。误打开的安装版已退出。未打包 DMG。
