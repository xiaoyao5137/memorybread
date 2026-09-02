# Temporal Evidence（阶段 A 草案）

本目录保存“观察时间、来源文档时间、事项发生时间”长期建设方案的实验性共享契约。当前状态为 `stage_a_draft`，没有被 MemoryBread 运行时代码引用，不会触发数据库迁移或改变创作结果。

## 不变量

1. `observed_at` 只表示系统获得内容的时间，不能回填 `event_time_*`。
2. `source_created_at/source_published_at/source_modified_at` 只描述来源文档，不能自动传播到其中的事项。
3. 事项时间必须绑定一个或多个 `evidence_refs`；没有依据时保持 `null`。
4. 模型生成的事项先进入 `shadow`；引用、时间、状态和作用域回证通过后才可 `published`。
5. `usage_decision` 依赖任务周期和用途，是创作时的临时判断，不是事项的永久属性。
6. 内容、事项和纠正均保留版本边界。再次观察相同内容不能改变事项时间或状态。

## 文件

- `temporal-evidence.schema.json`：阶段 A 的结构化契约草案。
- 详细语义、持久化和发布方案见 `doc/temporal-evidence-stage-a/`。

所有时间戳使用 Unix epoch 毫秒 UTC；日历日、自然周和相对日期解析必须另带 IANA 时区及精度。
