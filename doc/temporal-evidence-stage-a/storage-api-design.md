# 阶段 B 候选存储与内部 API 设计

状态：阶段 A 详细设计，未实施。本文用于评估可行性、迁移、索引和回滚，不授权创建正式数据库表。

## 1. 设计目标

1. 复用现有 `captures/timelines/bake_documents/bake_document_source_snapshots/data_snapshots` 正文和版本数据，不复制全库正文。
2. 新增通用事项层，不能用数值专用 `timeline_data_facts` 承载所有事项。
3. 相同内容重复观察幂等；来源变化、提取器变化和规则变化均可追溯。
4. 事项时间未知时为 null，不使用观察时间兼容填充。
5. 运行时可分别关闭双写、影子读、强制准入、最终复验和惰性回填。

## 2. 表模型

### `memory_source_versions`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK，ULID | 本地稳定版本 ID |
| `source_kind/source_id` | TEXT NOT NULL | 指向现有来源；对外 API 不暴露可枚举整数 |
| `version_hash` | TEXT NOT NULL | 规范化正文和结构哈希 |
| `parent_version_id` | TEXT NULL FK self | 同源上一个已知不同内容版本 |
| `content_ref_kind/content_ref_id` | TEXT NOT NULL | 指向 capture、文档快照、数据快照等不可变内容；不复制正文 |
| `source_created_at/source_published_at/source_modified_at` | INTEGER NULL | 来源文档时间，epoch ms UTC |
| `source_time_basis` | TEXT NOT NULL | native/verified_page/revision/unknown |
| `completeness` | TEXT NOT NULL | complete/partial/truncated/unverified |
| `created_at/updated_at` | INTEGER NOT NULL | 记录生命周期，不是业务时间 |

唯一约束：`(source_kind, source_id, version_hash)`。索引：同源版本、哈希、来源修改时间。`content_ref_*` 由 repository 按来源类型验证存在，不使用跨多表外键。

### `memory_source_observations`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK，ULID | 观察记录 |
| `source_version_id` | TEXT NOT NULL FK | 观察到的内容版本 |
| `observed_at` | INTEGER NOT NULL | 采集/刷新/导入时间 |
| `observation_kind` | TEXT NOT NULL | capture/refresh/import/retrieval |
| `origin_ref` | TEXT NOT NULL | 可回查的采集/任务标识 |

唯一约束：`(source_version_id, observed_at, observation_kind, origin_ref)`。同一内容再次观察只新增此表记录。

### `memory_claims`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK，ULID | 事项 ID |
| `semantic_key` | TEXT NOT NULL | 规范化对象、动作、上下文和口径哈希，仅用于候选去重 |
| `claim_text/subject` | TEXT NOT NULL | 规范化事项和对象 |
| `claim_kind/modality/status` | TEXT CHECK | 使用共享契约枚举 |
| `event_time_start/event_time_end` | INTEGER NULL | 事项区间，不回退观察时间 |
| `time_precision/time_basis` | TEXT CHECK | 精度及明确依据 |
| `decision_state` | TEXT CHECK | shadow/published/conflict/superseded/rejected |
| `contract_version/extractor_version/rule_version` | TEXT NOT NULL | 影响结果的全部版本 |
| `superseded_by` | TEXT NULL FK self | 被明确新版本事项覆盖时设置 |
| `created_at/updated_at` | INTEGER NOT NULL | 生命周期 |

不能对 `semantic_key` 建全局唯一约束：同主题不同时间、状态和来源可能是合法多个事项。索引覆盖发布状态、事件区间、语义键和规则版本。

### `memory_claim_evidence`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `id` | TEXT PK，ULID | 证据 ID |
| `claim_id/source_version_id` | TEXT NOT NULL FK | 事项和不可变来源版本 |
| `quote_hash` | TEXT NOT NULL | 最小逐字引用哈希 |
| `locator_json` | TEXT NOT NULL | section path、表格行或字符偏移 |
| `date_role/scope_kind` | TEXT CHECK | 日期角色和继承边界 |
| `validation_status/reason_code` | TEXT CHECK | pending/verified/rejected/conflict 及稳定原因 |
| `created_at/updated_at` | INTEGER NOT NULL | 生命周期 |

唯一约束：`(claim_id, source_version_id, quote_hash, date_role)`。引用正文从 `content_ref_* + locator_json` 动态读取；普通日志只写 reason code。

### `memory_claim_relations`

表达 `supports/contradicts/supersedes/derived_from`。唯一约束：`(from_claim_id, to_claim_id, relation_kind)`。系统不得仅凭观察时间创建 supersedes。

### `memory_claim_overrides`

保存用户对具体事项、来源版本和字段的纠正，包含旧值、新值、原因、创建和撤销时间。写入后不删除底层证据；撤销重新计算受影响用途缓存。新来源版本默认不继承覆盖，除非用户明确选择同源后续版本。

### `creation_claim_usages`

| 字段 | 说明 |
| --- | --- |
| `history_id/run_id/skill_step_id` | 创作与步骤范围 |
| `claim_id` | 被采用或排除的事项 |
| `purpose/period_start/period_end/timezone` | 用途判断输入 |
| `usage_decision/reason_code/rule_version` | 结果和规则 |
| `output_locator_json` | 最终文档中的标题、列表项或字符定位；排除项为空 |

唯一约束覆盖同一运行、步骤、事项、用途和周期。创作删除按现有历史生命周期处理；事项本身不因删除一次创作而删除。

## 3. 事务边界

1. 来源版本和观察记录在同一事务幂等提交。
2. 一次提取运行先写运行记录，再在一个事务写事项、证据和关系；全部校验成功后将合格事项切换为 published。
3. FTS/向量索引在事务提交后由 outbox-like 本地状态表推进，可从 published 事项重建。
4. 用户纠正、撤销和受影响事项失效在同一事务完成，缓存删除可重试。
5. 创作使用记录随创作历史保存；最终复验失败不得把运行标记为证据完整。

## 4. 内部 API 候选

API 仅在本机 core-engine/sidecar 边界使用。错误响应包含稳定 `code`、`trace_id` 和可读 message，不包含正文或私人 URL。

### 观察来源版本

`POST /internal/v1/temporal/source-versions:observe`

输入：来源标识、内容引用、哈希、观察时间、完整性和可选来源时间。返回 `source_version_id`、是否新版本、是否需要提取。相同幂等键和内容必须返回同一结果。

错误：`SOURCE_CONTENT_NOT_FOUND`、`SOURCE_HASH_MISMATCH`、`INVALID_SOURCE_TIME_BASIS`。

### 提交提取结果

`POST /internal/v1/temporal/source-versions/{id}/extractions`

输入：契约/提取器/规则版本、候选事项和证据定位。core-engine 重新检查 schema、引用哈希和事务唯一约束；sidecar 的“已验证”标记不能替代 core-engine 数据完整性检查。

错误：`SOURCE_VERSION_STALE`、`EVIDENCE_LOCATOR_INVALID`、`EVIDENCE_HASH_MISMATCH`、`CONTRACT_VERSION_UNSUPPORTED`。

### 检索事项

`POST /internal/v1/temporal/claims:search`

输入：查询、来源类型、任务周期、时区、用途、状态、limit 和 cursor。返回事项、最小来源摘要、三类时间、用途决定和 reason code；默认不返回引用正文。

未知/冲突作为显式结果，不使用空数组掩盖。后台补证只能返回 `retry_token`，不得在幂等检索请求中无限等待。

### 保存创作使用记录

`POST /internal/v1/temporal/creation-claim-usages:batch`

按 run/step 幂等保存采用和排除决定。最终文档定位可在保存历史后补写。

### 用户纠正与撤销

- `POST /internal/v1/temporal/claims/{id}/overrides`
- `POST /internal/v1/temporal/claim-overrides/{id}:revoke`

需要字段级旧值、新值、来源版本范围和理由。重复撤销幂等。

## 5. 兼容迁移顺序

1. 在 `shared/db-schema/migrations` 先增加新表和索引；不修改旧字段语义。
2. 在 core-engine migration runner 增加等价的兼容建表和 `add_column_if_missing` 防护。
3. 将新增表加入本地快照/备份白名单和恢复测试。
4. 发布只写 repository 和诊断命令，默认关闭双写。
5. 打开新内容影子双写；旧创作仍读取原表。
6. 影子读达标后才启用 `claims:search`，并按任务类型灰度。

不得执行以下迁移：

```sql
UPDATE memory_claims SET event_time_start = observed_at;
UPDATE timeline_data_facts SET period_basis = 'source_period';
```

旧 `timeline_data_facts.period_*` 由观察时间产生时，读取适配器将其标为 `observation_bucket`；只有来源明确统计周期才产生 `source_statistical_period`。

## 6. 功能开关

建议使用稳定的用户可见 feature flag 投影，内部细分开关不暴露：

- `temporal_claim_shadow_write`
- `temporal_claim_shadow_read`
- `temporal_claim_enforcement`
- `temporal_claim_final_validation`
- `temporal_claim_lazy_backfill`
- `temporal_claim_overrides_ui`

关闭读取/强制开关不删除新表。数据一致性或高严重度误纳时立即回旧读取，保留影子写入供诊断；迁移本身只前向修复，不运行破坏性降级 SQL。

## 7. 索引与容量验证

阶段 B 在脱敏副本上测量：每来源版本平均事项数、证据定位大小、FTS/向量索引增长、备份体积、重建时间和写放大。引用不重复存正文，预计主要增长来自事项文本、定位和索引；任何容量承诺必须以后续实测报告为准。

## 8. 安全和隐私

- 全部新数据保留在本机现有数据库和备份边界。
- 日志、trace 和错误详情禁止写 prompt、正文、引用、私人 URL、用户纠正和密钥。
- 来源文本中的指令只作为不可信资料，不触发工具或策略变更。
- 外部模型能力若被用户显式启用，仍须遵守现有授权边界；阶段 B 默认使用本地提炼能力。
