# 受众身份与交付目标限定兼容复核

日期：2026-09-08。复核者：frontend_qa。

本次根代理明确重新授权，仅复核已实测的“未知受众通知被补写为各位同事”缺陷。原资料范围与恢复边界结论保留于 `final-source-resume-review.md`，本次不重写其结果。

方式：只读源码、现有与新增测试和相关差异；未修改生产代码或测试，未调用模型、未操作 CUA。相关 141 项回归与真实混合请求验收由 operations_qa 执行，本文不将待运行结果表述为已通过。

## 最终结论

限定范围内未发现剩余兼容阻断。

- 未知受众不再默认成业务或技术群体，也不以主题中出现的人群词自动推断受众。显式 `CreationOptions.audience` 保留；原请求中的明确受众仍在用户原文中。
- `assess_inputs` 通过原有解码校验后，将运行时 `deliverable` 绑定原始 instruction。未发现该字段参与路由或工具选择的代码消费者；HTTP 和合同 schema 保持原样。
- 原 instruction 继续保留于 `supplied_lines` 和审查的用户材料；其中给定的时间、地点、身份等事实没有随目标字段处理而被剔除。
- Writer 只附加原请求与验收条件，消费实际来源和回执，不再把模型代写的 deliverable 或检索前的分析 reason 当成现有事实。
- Writer 和 Reviewer 规则明确区分用户材料与模型分析。无依据的受众身份、组织关系和适用范围不能由文体惯例补出；明确身份、普通礼貌邀请和用户授权的虚构创作保留。

## 复核发现并补齐的兼容路径

首版只保留显式 audience 于任务画像，独立 Reviewer 无法知道该设置来自用户。已补齐 `user_options`：仅从 `state.options.audience` 读取；在 assessment 中形成 `user-options-audience-1` 事实行，Writer 和 Reviewer 使用同一权威字段。

旧 `input_context v1` 只补充或刷新此新增字段，不重算或覆盖已有 root、conversation、brief，也不从推断的 requirement 获取受众。新增内部参数有默认值，不增加公共 HTTP 参数。

新增测试覆盖：未知受众与主题人群词；显式选项受众；模型代写交付草稿被替换；原指令中的事实、明确身份和虚构要求保留；Writer 排除旧预检推断；显式受众在 assessment、Writer、Reviewer 中一致；空受众不产生身份；旧 v1 恢复设置且原材料不变。测试包含实际 Loop 上下文传递的正对照，并非仅检查提示词文本。

本次检查的相关差异通过 `git diff --check`。语义效果以 operations_qa 的回归与真实 mixed 请求结果为最终验收依据。
