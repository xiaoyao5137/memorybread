---
name: obsidian-export
description: Export selected MemoryBread memories into a local Obsidian vault as Markdown notes with YAML frontmatter, supporting marquee/range multi-select and idempotent incremental updates.
---

# Obsidian 本地导出

## 能力边界

本 Skill 只读取用户在执行工作台中圈选或勾选的本机记忆，只写入用户通过目录选择器明确选定的 Vault 目录下的 `MemoryBread` 子目录。不连接 Obsidian 云服务，不触碰 Vault 内其他目录与文件，也不把记忆正文写入执行日志。

## 输入

- `memoryIds`：用户圈选的本机 timeline 记忆 id 列表（1–200 条）
- `vaultPath`：用户选定的 Obsidian Vault 绝对路径
- `subfolder`：Vault 内归集子目录，默认 `MemoryBread`

## 执行工作流

1. `preview`：校验所选记忆与 Vault 路径，返回计划写入的笔记列表与将被覆盖的文件数，不写盘。
2. `execute`：把每条记忆写成一篇 Markdown 笔记，文件名由标题清洗生成，冲突时追加短哈希。
3. 每篇笔记携带 YAML frontmatter，含 `memorybread_id` 与 `memorybread_content_hash` 作为幂等身份。
4. 正文与标题均未变化时记为 `unchanged`，不重写；变化则只覆盖对应笔记，记为 `updated`。

## 输出

- `created`、`updated`、`unchanged` 数量
- 每篇笔记的 id、标题与 Vault 相对路径
- 可审计运行状态与脱敏日志

## 验收

同一批记忆连续导出两次时，第二次不得重复创建、不得改写未变化的笔记；修改一条记忆后，只允许该笔记更新一次。
