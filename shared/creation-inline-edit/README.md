# Creation Inline Edit v1

`creation.inline-edit.v1` 是创作文档划选脑暴、润色、扩充与细化的共享契约。

## 边界

- Markdown 是唯一正文数据源。
- 页面提交 UTF-8 字节范围、基线文档哈希和选区哈希；Core 必须和当前历史复验。
- Sidecar 只返回候选 `replacement_markdown`，Core 负责合成完整文档、生成 Patch 并落库。
- 请求动作使用 `brainstorm | polish | expand | elaborate`；历史/Patch 使用 `brainstorm_selection | polish_selection | expand_selection | elaborate_selection`。
- `custom_prompt` 允许用于 `polish`，或承载用户已经确认的局部脑暴结论；局部脑暴未收敛前不得写回正文。
- 本地模式不产生模型内容出站；品牌模型模式只发送选区、最小章节上下文、动作、用户自定义润色要求和允许约束。

## 哈希与范围

- 内容哈希统一为 SHA-256 小写十六进制。
- `start_byte` 包含，`end_byte` 不包含，两端必须位于 `current_document` 的 UTF-8 字符边界。
- 操作指纹由 Core 对不可变语义字段的规范化 JSON 计算；`resume_state` 和 `model_result` 不参与。

## 发布门禁

- DOM 到 Markdown 的映射无法无损往返时不显示工具条。
- 支持跨标题、段落、列表项和引用块的连续选区；起止边界必须能无损映射回 Markdown。
- 表格、代码、链接地址、图示源码、内部标记和跨复杂行内格式明确拒绝。
- 替换片段保持原选区中完整的强调结构。原选区没有 `**` 时，模型自行新增的加粗标记会被确定性移除；历史内容已经包含不成对的 `**` 时仍允许用户划选，并在下一次选区编辑中清理损坏标记。
- Skill 文档无法恢复结构不变式时禁用选区编辑。
- 扩充和细化只允许使用当前章节与历史证据提供的事实；新增数字、日期、URL 或引用必须已经存在于允许约束中。
- 历史与页面中的用户指令必须记录动作的完整语义、选取内容摘要和用户补充要求，不能只保存“润色”“扩充”或“细化”等动作名。
