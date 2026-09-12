# 同一选项多道延展问题的前端验收

日期：2026-09-10。

前端继续消费 `current_question` 和 `history`。每道题有独立 ID，`parent_question_id` 与 `parent_option_id` 可相同；思路树现有数组聚合已经支持同一选项下的多道同层题。后台计划、并发和缓存由 Core 负责，前端没有新增待答选择或自动提交行为。

## 自动回归

新增 `desktop-ui/src/__tests__/CreationBrainstormSiblingQuestions.test.tsx`，覆盖：

- 同一选项的第一题提交后切换到第二题，即使选项 ID 相同也清空本地选择；第二题仍需要用户明确确认。
- 上一题/下一题保留正确答案与共同父分支；只读回看不发出新的 answer 请求。
- 树内两道题处于同一父选项和同一深度，查看未答题不形成简报决定。
- 上游回改后的失效同层题可回看，但不进入当前确认数。

运行新增测试以及既有 `CreationBrainstormBranch`、`CreationBriefEditor`、`CreationPanelBrainstorm` 回归，共 4 个文件、65 条测试通过。既有测试输出有 React act 与 Node localStorage 警告，无失败。`npm run build` 完成版本同步、TypeScript 与 Vite 生产构建，退出码为 0。

## 实际浏览器操作

使用 CUA 在 Codex 内置浏览器访问 `http://localhost:1420/test/brainstorm-siblings-preview.html`。该 fixture 在应用模块加载前隔离 localStorage，所有 fetch 均模拟，只使用虚构社区阅读活动及模拟答案，不连接真实用户会话或推理服务。

实际执行并观察：

1. 在“共读内容”选择“同读一篇短文”并确认，进入同一“共读与交流”方向下的“交流形式”；新题选项均未选中，确认按钮禁用，提交计数为 1。
2. 回看上一题，已提交选项正确勾选；返回当前题后选项仍未选中，提交计数保持 1。
3. 展开思路树，查看“交流形式”节点；父题仍为活动方向根题，所属分支为“共读与交流”，节点详情完整呈现，未答选项没有确认标记。
4. 在 390 × 844 窄屏滚动到题目选项并选择“小组轮流交流”，确认后进入另一同层方向“参与体验”；新题仍未选中，提交计数变为 2。
5. 实测窄屏 document 宽度为 390，与 viewport 相同；题卡 scrollWidth/clientWidth 均为 352，节点详情均为 282，没有横向内容溢出。截图检查了默认宽屏和窄屏的真实渲染，完成后恢复默认 viewport。

此证据证明现有前端能够消费并展示同父选项的多道问题，不证明真实模型问题质量、后端调度顺序或推理耗时；这些需要独立后端与真实模型验收。fixture 按固定顺序提供示例问题，只用于 UI 消费和操作验证。
