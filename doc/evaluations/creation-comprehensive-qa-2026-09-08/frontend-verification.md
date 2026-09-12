# 创作前端 QA 与根因修复

2026-09-08；仅修改 CreationPanel.tsx、CreationBriefEditor.tsx、CreationBriefEditor.test.tsx 与新增 CreationPanelSessionIsolation.test.tsx。保留原工作区改动，没有提交 Git、没有改 Core/Python、没有触发 DMG。

## 已修复的问题

1. 运行时恢复另一条历史记录会保留旧 Agent 请求；旧 SSE、建档响应、终态保存和保存失败回退可能污染新会话。加入会话/运行代次及 controller 归属检查，恢复时先停止并持久化原运行，晚到保存不重绑目标历史，失败回退固定使用原会话快照。旧 finally 不再清理新运行状态。源码 CreationPanel.tsx:1548、2126、3794、4460、4520、4570、4753。
2. 局部编辑期间新建/切换/终止会话没有立即释放运行状态，迟到的 no_change/committed/撤销响应可能写入新文档。统一清理和中止控制器，收口服务端暂停运行，所有异步提交/恢复/撤销都校验原请求仍拥有当前会话。源码 CreationPanel.tsx:2108、2844、3183、5249。
3. 用户取消局部编辑时，即使服务端返回 409（提交中/已提交），前端也无条件 abort，可能丢失已经提交的文档响应。现在仅确认取消后终止并显示取消消息；409 保留原请求继续同步。源码 CreationPanel.tsx:3150。
4. 服务端已提交，但本地校验与历史恢复失败时仍提示“文档未修改”。现在明确提示“修改已提交，页面同步失败”，关闭旧版本选区能力，指引打开历史最新版本。源码 CreationPanel.tsx:3117。
5. 父题取消选择某方向后，该方向已归档的子题被 UI 过滤掉，无法回看。保留有历史子题的原分支并标记，原题/答案继续可查。源码 CreationBriefEditor.tsx:34。
6. 简报保存时离开编辑器，迟到成功回调可能清空另一会话的简报草稿。组件卸载后不再回写草稿；脑暴错误响应也在解析后校验请求归属，防止旧 revision-conflict 重试复活旧会话。源码 CreationBriefEditor.tsx:14、50，CreationPanel.tsx:3974。

## 新增回归

CreationPanelSessionIsolation.test.tsx:76/130/166/212 共 10 项，覆盖建档、Agent SSE、终态保存、失败回退、no_change/committed、撤销、已提交但恢复失败、取消 200/409。
CreationBriefEditor.test.tsx:45/56 共 2 项，覆盖父选项取消后的归档分支可达与离开编辑器后的迟到保存。

把最终新增测试复制到原始基线副本，12 项全部失败，原有 4 个简报用例通过。当前 12 项全部通过。

## 验证

- 基线创作专项：19 文件，233/233 通过。
- 首轮修复后创作专项：20 文件，245/245 通过；追加复审后的最终结果为下文 247/247。
- npm run build：版本检查、TypeScript、Vite 均通过；保留既有较大 chunk 警告。
- git diff --check：任务范围通过。
- 追加最后 2 项回归之前的全前端快照：90 文件，89 通过/1 失败；753 用例，749 通过/4 失败。
- 4 个失败均位于 SettingsDebugMode.test.tsx：旧断言固定 fetch 调用 2 次，当前实际 3 次。原始基线副本和当前代码各单独复测均相同 4/4 失败，未修改 Settings 或相关测试。该独立缺陷不属于本次创作回归。

本子任务未操作共享浏览器，也没有宣称完成真实模型内容验收。由主 agent 统一执行真实 UI、Core/Sidecar 和成稿验收。

## 证据

- frontend-new-regressions-baseline.log：新增回归在原始副本 12 失败/4 通过。
- frontend-creation-final.log：首轮修复后创作 245/245。
- frontend-build-final.log：构建通过。
- frontend-full-final.log：追加最后 2 项回归之前的全前端快照 749 通过/4 失败。
- frontend-settings-baseline.log / frontend-settings-current.log：独立基线已存在失败对照。
- frontend-panel.patch / frontend-brief.patch：相对任务开始时源文件的纯本次修改。

## 最终差异复审追加（03:08）

复审又发现并实际复现 2 项，仍只修改上述 4 个文件：

7. 脑暴失败重试在新 run 首事件到达前中止时，终态判断可能引用用户消息上的旧失败 runIds，遗漏本轮 cancelled 落库。现用本次请求独立的 activeAgentRunIdRef；首事件前取消创建本轮独立取消记录，不覆盖上一轮失败轨迹。源码 CreationPanel.tsx:1589、3655、4778、5102；回归 CreationPanelSessionIsolation.test.tsx:256。
8. 当 /agent/run 返回 404 回退到旧版生成接口时，其迟到本地流/参考加载/云端结果仍能覆盖新会话。兼容路径现在复用相同的本次执行活跃校验，并把正常失败交回统一错误收口。源码 CreationPanel.tsx:3244、4722、4913；回归 CreationPanelSessionIsolation.test.tsx:292。

证据 frontend-review-before.log：追加 2 项失败、已有 10 项通过；frontend-review-after.log：12/12 会话隔离用例通过。

最新创作专项 frontend-creation-reviewed.log：20 文件，247/247 通过（相对最初新增 14 项）。frontend-build-reviewed.log：版本检查、TypeScript、Vite 构建通过。diff --check 通过。

新增测试以真实组件交互驱动，以目标文档、会话保存载荷、取消终态和历史轨迹为断言；没有仅比对新增内部控制器/函数的实现。最后差异审核未再发现本次改动造成的阻断问题。此前整个前端 749/753 的结果和 Settings 基线对照继续保留为先前一轮证据。
