# 集成 Skill 默认召回与 Codex 中文 @ 排查

日期：2026-09-06。

## 默认召回变更

- WorkBuddy、Codex、Claude Code 的 Skill 示例与默认指导统一为 `top_k=10`。
- Codex / Claude Code 脚本默认值与帮助信息同步为 10；WorkBuddy 导出复用 Codex 脚本。
- 保持显式参数范围 1–10、每条正文 3000 字符及原有重试规则。
- 本机已安装的 Codex 和 WorkBuddy Skill 已同步；未发现标准路径下的 Claude Code 安装版，仅更新仓库模板。
- 本机旧版备份：`/Users/xianjiaqi/.codex/skill-backups/memorybread-20260906-122633`。备份放在 Skill 扫描目录之外。
- Rust 集成导出使用 `include_str!` 嵌入模板，因此已编译客户端的导出内容需在下次重新构建后更新。本次未打包客户端。

验证：`node --test test/memory-retrieval-skill.test.mjs`，8 项通过。新增测试验证省略参数时请求并返回 10 条、显式 3 条仍有效、11 条在发请求前被拒绝。Codex / Claude Code 通过 Skill Creator 校验；WorkBuddy 单独校验 YAML 并保留平台原有 `agent_created` 字段。

真实服务验证：本机安装版在不传 `--top-k` 时执行召回，返回 `SERVICE_UNAVAILABLE`。因此本次证明默认参数与脚本协议正确，未完成真实记忆结果数量验收。

## 中文 @ 根因

检查对象：本机 `com.openai.codex`，版本 `26.901.51231`，构建 `8109`，位于 `/Applications/ChatGPT.app`。仅只读分析应用资源，未修改应用。

通过应用内置 `codex app-server --stdio` 的 `skills/list` 获取当前 Skill，结果包含：

```json
{
  "name": "memory-retrieval",
  "interface": { "displayName": "记忆检索" },
  "enabled": true
}
```

没有顶层 `displayName`。这说明安装及 YAML 名称解析正常。

应用资源 `webview/assets/app-initial-cadb12d4a15e.js` 中，`Txi` 加载 Skill 列表；`Exi` 只展平并按路径去重，不将 `interface.displayName` 映射到顶层。显示名称函数 `lnr` 正确读取 `e.interface?.displayName`。

但统一 @ 选择器位于 `webview/assets/app-primary-6cd7b8b3f5e3.js`：`iJt` 经 `sJt` 排序，再用 `lJt` 过滤。排序函数 `cJt` 与过滤函数 `lJt` 都读取顶层 `displayName`。过滤实现如下（保留原逻辑，展开格式）：

```js
function lJt(skill, query) {
  const needle = query.trim().toLowerCase();
  return needle.length === 0 || [
    skill.name,
    skill.displayName ?? "",
    `@${skill.displayName ?? skill.name}`,
  ].some(value => value.toLowerCase().includes(needle));
}
```

从本机包提取并实际执行此函数，输入上述数据：

| 查询 | 当前实现 | 将 interface.displayName 映射到顶层后 |
| --- | --- | --- |
| 记忆 | false | true |
| 记忆检索 | false | true |
| memory | true | true |
| memory-retrieval | true | true |

结论：当前版本统一 @ 选择器的 Skill 名称字段不一致，导致中文显示名被过滤。不是中文 YAML 无效，也不是 Skill 未安装。上轮提出“改双语显示名”不能修复此客户端缺陷，应撤回作为直接修复的建议。

建议上游统一使用显示名称解析函数，在排序、过滤、渲染三处读取一致字段，并对仅含 `interface.displayName` 的 Skill 添加中文部分匹配和英文内部名匹配测试。仅修改显示名仍不能恢复该版本的中文 @；不应添加无效 YAML 别名字段或修改已签名客户端包。

临时使用 `@memory-retrieval` 选择该 Skill，或在自然语言中明确要求使用记忆面包检索。后者属于模型路由，不等于修复选择器。
