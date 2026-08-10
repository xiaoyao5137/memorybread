# MemoryBread 服务端产品技术方案

> 版本：v1.0  
> 日期：2026-06-30  
> 适用范围：`mb-admin`、`mb-gateway`、账户计费、云端快照、内部运营平台  
> 建设策略：面向中国大陆个人用户，模块化单体 MVP，保留后续拆分服务的边界  
> 云基础设施：服务端数据库与对象存储统一使用阿里云托管服务，不自建本地数据库或对象存储集群

---

## 1. 方案摘要

MemoryBread 当前是一款以本地数据为核心的 AI 桌面工作助手，现有客户端由 Tauri + React/TypeScript、Rust core-engine 和 Python ai-sidecar 组成。服务端的价值不是把所有数据“云化”，而是在不破坏“本地优先、隐私可控”产品定位的前提下，补齐四项能力：

1. **商业化**：账户、订阅、充值、Credit 计量、订单与发票。
2. **远程模型**：客户端只认识 MemoryBread 品牌模型，网关统一路由、限流、计量、结算与降级。
3. **多设备可迁移**：客户端加密后上传版本化快照，新设备可恢复，服务端默认无法解密用户内容。
4. **用户与运营可见性**：用户能查看余额、消费、调用和快照；运营人员能查看成本、毛利、路由、故障和风控。

推荐将产品面拆成：

| 产品面 | 建议域名 | 定位 |
| --- | --- | --- |
| 官网 | `www.memorybread.com` | SEO、产品介绍、下载、定价、安全说明 |
| 用户控制台 | `app.memorybread.com` | 钱包、用量、云存储、设备、安全、订阅 |
| 内部运营台 | `ops.memorybread.internal` | 用户、订单、Credit、模型路由、成本、风控、审计 |
| 业务 API | `api.memorybread.com` | 账户、计费、快照和控制台 API |
| 模型网关 | `gateway.memorybread.com` | MemoryBread 客户端、OpenAI/Anthropic 兼容调用 |

`mb-admin` 可以保留为单一代码库，但官网、用户控制台和内部运营台必须有独立路由分区、权限边界和发布策略。

---

## 2. 产品原则与边界

### 2.1 核心原则

- **本地优先**：本地模型、本地数据库和本地知识库仍是默认路径。
- **云服务托管**：“本地优先”仅指 MemoryBread 桌面客户端的个人记忆数据。`mb-admin`、`mb-gateway` 和 worker 的业务数据统一使用阿里云 RDS PostgreSQL，快照密文与文件资产统一使用阿里云 OSS；服务节点不保存持久化业务数据。
- **云能力可选**：用户可只使用本地功能，不强制开启云模型或云快照。
- **品牌模型稳定**：客户端只传 `brand_model_id`，真实供应商与模型由服务端路由。
- **账务可追溯**：订单、支付、Credit 和模型用量通过不可变事件与账本关联。
- **最小内容留存**：模型网关默认不持久化 prompt、回答、截图和文档正文。
- **先恢复，后合并**：MVP 云存储是版本化备份/恢复，不冒充实时多端协作。

### 2.2 MVP 不做的事

- 不在服务端运行用户的本地 RAG 数据库。
- 不在多设备之间实时合并 SQLite 行级数据。
- 不允许运营人员查看用户快照明文或模型请求明文。
- 不直接对客户端暴露供应商 API Key、真实模型名或购买成本。
- 不在 MVP 引入 Kafka、Kubernetes 等运维负担较高的基础设施。
- 不在 ECS/容器内自建 PostgreSQL、Redis、MinIO 或依赖本地持久化卷；服务端业务进程必须保持无状态。

---

## 3. 用户与关键业务流程

### 3.1 用户角色

| 角色 | 主要权限 |
| --- | --- |
| 访客 | 浏览官网、定价、安全说明与下载页 |
| 免费用户 | 登录控制台、查看基础用量、领取赠送 Credit、使用有限云快照 |
| 订阅用户 | 获得周期性 Credit、更高并发、更大存储和更长快照保留 |
| 客服 | 查看用户账户状态、订单与脱敏调用元数据，不得修改模型路由 |
| 运营 | 配置套餐、优惠、公告、赠送 Credit，不得读取供应商密钥 |
| 财务 | 查看订单、支付、退款、对账和发票 |
| 网关管理员 | 配置供应商、路由、价格和灰度规则 |
| 审计员 | 只读查看管理操作与敏感变更记录 |

### 3.2 注册与设备绑定

#### 注册与登录方式

- **手机号**：支持手机号 + 短信验证码注册/登录，中国大陆号码首期使用阿里云短信服务。
- **邮箱**：支持邮箱 + 邮件验证码注册/登录；如后续开放密码，密码仅是额外凭证，不取代邮箱所有权验证。
- **第三方账号**：支持微信、QQ 授权登录与登录后绑定，后续可通过统一 provider adapter 增加 Apple 等渠道。
- **账号联合**：一个 `user` 可关联多个已验证 `identity`，但同一手机号、邮箱或第三方 provider subject 在同一环境中只能属于一个有效用户。

手机号入库前统一标准化为 E.164 形式；邮箱保留用户展示形式，同时维护用于唯一性比较的规范化值。第三方平台没有返回手机号/邮箱时，不得根据昵称或头像猜测是同一用户。

注册资料包含不可变账户名 `username`、用户展示昵称 `nickname` 和可选公司名称 `company_name`。客户端用户卡片、个人页标题与头像文字优先使用昵称，账户名仅作为灰色辅助信息保留展示。昵称与公司名称按字段分别限次，每个 UTC 自然月各最多实际修改 3 次；注册初值及同值保存不计次数。

#### 邀请码关联

- 注册页提供可选邀请码输入框，支持链接/QR 携带 `invite_code` 并在用户确认后关联。
- 一个新用户只能关联一个首次邀请关系。邀请码在账户完成手机或邮箱验证后原子绑定，默认不允许用户自助更换。
- 邀请码不直接存储“赠送 100 Credit”等业务字段，而是关联活动与版本化奖励规则。
- 奖励触发可配置为：注册验证完成、首次登录客户端、首次付费、订阅满 N 天等。
- 奖励类型支持：促销余额、Credit grant、月度/年度套餐、限时权益、优惠券；同一规则可分别奖励邀请人和被邀请人。
- 促销余额必须与可退/可提现现金余额分账户记录，默认不可提现，并允许配置有效期和适用商品。
- 奖励通过幂等 `reward_grant` 发放并落入对应账本/订阅记录，不直接更改余额数字。

#### 账号合并与设备绑定

1. 首次通过已验证手机号、邮箱、微信或 QQ 登录且身份未关联现有用户时，创建新用户与 identity。
2. 已登录用户可在“账户与安全”中绑定其他登录方式，绑定前要求当前会话二次验证。
3. 目标身份已属于另一账户时，不得静默合并；用户需分别证明两个账户的控制权，再进入可回滚的显式合并流程。
4. 合并前预览订阅、Credit/促销余额、设备和云快照处理结果；两个有效订阅冲突时不自动合并，转客服审核。
5. 桌面客户端使用系统浏览器完成 OAuth/device authorization，通过一次性 state + PKCE + 已注册 deep link 回到客户端。
6. 授权成功后客户端获得短期 access token 与可轮换 refresh token，并生成设备密钥对，只上传公钥、设备名称、平台、应用版本和最后活跃时间。
7. 用户可在控制台吊销单个设备，吊销后其 refresh token 和快照上传会话失效。

### 3.3 手机验证码流程

```text
提交手机号/用途 -> 号码规范化 -> IP/设备/手机号限流
    -> 必要时人机验证 -> 生成本地 challenge 和验证码哈希
    -> 服务端调用阿里云 SendSms -> 记录 RequestId/BizId 和提交结果
    -> 用户提交验证码 -> 常量时间比较 -> 单次消费 challenge
    -> 注册/登录/绑定的领域事务
```

实现规则：

- 阿里云短信 AccessKey 仅存放在 Secret Manager/KMS，使用最小权限 RAM 身份，前端和桌面客户端不得直接调用短信 API。
- 使用已审核的验证码签名和模板，生产与测试配置彻底隔离。
- 验证码建议 6 位、5 分钟有效、最多验证 5 次，只保存带 server pepper 的哈希，不写入日志或分析系统。
- 同一手机号默认 60 秒内不重复发送，并配置每小时/每日上限；同时对 IP、设备指纹、ASN 和失败率限制。具体阈值由风控配置，不写死在前端。
- 阿里云 `SendSms` 不提供业务幂等保证，MemoryBread 必须为同一 `sms_challenge_id` 做本地幂等；超时后先查回执/本地状态，不盲目重试造成重复短信。
- 请求发送和验证接口返回统一文案，不泄露手机号是否已注册。
- 验证码用途必须绑定为 `register/login/bind_phone/unbind_phone/account_merge/sensitive_action`，不允许跨用途复用。
- 支持阿里云短信发送状态回执，用于到达率、错误分布与成本监控；回执不改变已完成的用户鉴权事务。

### 3.4 远程模型调用

```text
客户端脱敏 -> 设备鉴权 -> 套餐/余额检查 -> 预估与冻结 Credit
    -> 品牌模型路由 -> 供应商流式调用 -> 用量标准化
    -> 实际结算/解冻 -> 写入用量与账本 -> 返回账单摘要
```

同一个用户操作可能触发多次模型调用，例如“检索 -> 重排 -> 创作”。所有子调用使用不同 `event_id`，但共享同一 `trace_id` 和业务 `request_id`。

### 3.5 云快照上传与恢复

```text
本地 SQLite Online Backup + 必要资产导出
    -> 生成 manifest -> 分块、压缩、客户端加密
    -> 申请 STS 凭证/分片上传 -> 直传阿里云 OSS -> 校验并提交快照
    -> 另一设备下载 -> 本地解密与校验 -> 兼容性迁移 -> 原子切换
```

---

## 4. `mb-admin` 产品设计

## 4.1 官网

### 4.1.1 官网目标

官网需在首屏回答三个问题：MemoryBread 是什么、为什么值得安装、用户的数据是否安全。主要转化目标为“下载客户端”，次要目标为“查看定价”和“登录控制台”。

### 4.1.2 页面地图

| 页面 | 路由 | 核心内容 |
| --- | --- | --- |
| 首页 | `/` | 产品定位、产品动画、场景、差异化、隐私、定价、下载 |
| 场景 | `/use-cases` | 历史回忆、文档创作、知识沉淀、任务自动化 |
| 安全与隐私 | `/security` | 本地优先、脱敏、云快照加密、数据删除、子处理者 |
| 定价 | `/pricing` | Free/Plus/Pro 比较、Credit 说明、FAQ |
| 下载 | `/download` | macOS/Windows 版本、签名校验、更新日志 |
| 文档 | `/docs` | 安装、上手、迁移、故障排查 |
| 博客/更新 | `/blog` | 产品更新、案例、本地 AI 内容 |
| 状态 | `/status` | API、网关、快照和供应商状态 |

### 4.1.3 首页信息架构

1. **顶部导航**：Logo、场景、安全、定价、文档、登录、“免费下载”。
2. **Hero 首屏**：
   - 主标题：“让 AI 真的记住你的工作”。
   - 副标题：自动沉淀工作上下文，基于真实历史完成问答与文档创作，数据默认留在本地。
   - CTA：“免费下载”、“查看 90 秒演示”。
   - 平台与信任标识：macOS/Windows、本地优先、可选云同步。
3. **核心产品 GIF**：展示“继续上周未完成的竞品分析”的完整闭环。
4. **问题共鸣**：看过就忘、重复交代背景、文档风格无法复用、知识散落。
5. **三步工作原理**：感知 -> 沉淀 -> 唤醒/创作。
6. **场景卡片**：历史回忆、方案生成、周报复盘、SOP 提炼、个人知识库。
7. **隐私设计**：本地数据库、敏感窗口跳过、上云前脱敏、快照客户端加密。
8. **本地与云端能力对比**：明确哪些功能免费本地运行，哪些使用 Credit。
9. **定价摘要**：月付/年付切换、套餐 Credit、存储和设备数。
10. **FAQ 与底部 CTA**：“我的屏幕内容会被上传吗？”应作为第一个问题。

### 4.1.4 GIF/视频演示脚本

| 编号 | 时长 | 内容 | 目标 |
| --- | ---: | --- | --- |
| G1 历史回忆 | 10–12s | 输入“我上周在哪个页面看到过这个定价？”，返回时间线与来源 | 展示长期记忆 |
| G2 文档创作 | 12–15s | 选择“技术方案”模板，自动引用项目历史生成大纲 | 展示直接产出 |
| G3 自动沉淀 | 8–10s | 多个工作片段被归纳为知识条目 | 展示无感处理 |
| G4 隐私控制 | 8–10s | 将应用加入黑名单，隐私统计即时更新 | 建立信任 |
| G5 跨设备迁移 | 12–15s | 旧电脑上传加密快照，新电脑选择版本恢复 | 展示云快照价值 |

制作规范：

- 优先 WebM/MP4，GIF 仅作降级，避免首页大体积。
- 宽高比统一为 16:10，与桌面应用视觉一致。
- 每个动画提供静态 poster、延迟加载和 `prefers-reduced-motion` 降级。
- 演示数据使用虚构项目，不出现真实用户屏幕内容。

### 4.1.5 视觉与 HTML 实现规范

- 本节仅约束官网视觉：延续 MemoryBread Logo 的面包色系，使用暖白背景、深棕文字、烘焙橙主色，避免“通用 AI 蓝紫渐变”；不得据此覆盖桌面客户端各页面已有的设计系统。
- 整体语言应是“可信赖的工作搭子”，不使用夸张智能、代替人类等表述。
- 语义化 HTML：`header/nav/main/section/article/footer`，所有交互支持键盘与清晰 focus state。
- 首屏 LCP 不依赖视频资源，视频在首屏文字可用后加载。
- 官网使用 SSR/SSG，每个场景有可索引文字，不将关键信息只放在图片中。
- 埋点只记录页面、CTA 和转化事件，不采集输入内容。

### 4.1.6 官网验收指标

- Lighthouse Performance/Accessibility/SEO 目标均不低于 90。
- 首屏主 CTA 无滚动可见，下载系统能正确识别操作系统与 CPU 架构。
- 在 JavaScript 失败时，核心产品介绍、安全说明和下载链接仍然可用。
- 产品动画不造成明显布局偏移，低速网络不自动加载高码率视频。

## 4.2 用户控制台

### 4.2.1 整体导航

```text
概览
钱包与套餐
  ├─ 资金/Credit
  ├─ 套餐与订阅
  └─ 订单与发票
使用分析
  ├─ 用量趋势
  └─ 调用流水
云存储
  ├─ 快照
  ├─ 设备
  └─ 恢复记录
账户与安全
通知设置
```

### 4.2.2 概览页

概览页应将“余额、套餐、用量、快照健康”放在一屏内：

- Credit 可用余额、已冻结余额、预计可用天数。
- 当前套餐、周期截止时间、下次扣款金额。
- 今日/30 天调用数、token、Credit 和失败率。
- 最近一次快照、快照大小、已使用存储与备份异常。
- 待处理卡片：余额不足、支付失败、快照连续失败、新设备登录。

### 4.2.3 钱包管理页

#### 余额表达

产品上必须区分两种余额：

- **资金余额**：用户实际充值的货币余额；是否开放需根据支付与预付款合规确认。
- **Credit 余额**：用于模型调用的产品计价单位，区分购买、套餐赠送、活动赠送及各自有效期。

为降低 MVP 的账务与合规复杂度，建议首期不提供可长期沉淀的“资金钱包”，而是支付后直接获得 Credit 或订阅权益。控制台可将“资金余额”作为二期功能开关。

#### 页面模块

- 总 Credit、可用、冻结、近 30 天消耗、本周预测。
- 余额构成：购买 Credit、套餐 Credit、赠送 Credit，展示最近到期批次。
- 快捷充值：固定档位 + 自定义数量，确认页显示价格、Credit、有效期与退款规则。
- 自动充值：余额低于阈值时发起代扣，MVP 可只提供余额通知。
- Credit 流水：来源、变动数量、变动后余额、关联订单/调用、时间、状态。
- 导出：CSV 导出和按月账单下载。

### 4.2.4 套餐与订阅页

建议首期提供三档，具体价格与额度由运营配置，不写死在前端：

| 权益 | Free | Plus | Pro |
| --- | --- | --- | --- |
| 本地核心功能 | ✓ | ✓ | ✓ |
| 周期 Credit | 少量体验 | 中等 | 高额 |
| 远程模型并发 | 1 | 2–4 | 5–10 |
| 云快照存储 | 基础 | 扩展 | 高额 |
| 快照保留 | 最近 1–3 个 | 30/90 天 | 180/365 天 |
| 设备数 | 1–2 | 3 | 5+ |
| 支持 | 社区 | 优先 | 优先 |

支持月付、年付，年付展示等效月价和节省金额。升级立即生效并按剩余周期补差；降级下个周期生效；取消后权益保留到周期结束。

### 4.2.5 使用日志页

#### 顶部指标

- 总调用数、成功率、输入 token、输出 token、Credit、P95 延迟。
- 与上一等长周期对比，明确箭头是“增长”而非“好/坏”。

#### 趋势图

- 时间粒度：小时/天/周，适配 24h、7d、30d、90d 和自定义范围。
- 可切换指标：调用数、token、Credit、失败率、延迟。
- 可分组：品牌模型、调用场景、设备。
- 不将不同量纲的 token 和 Credit 默认叠在一个 Y 轴。

#### 调用流水

| 字段 | 说明 |
| --- | --- |
| 时间 | 请求开始时间，支持时区切换 |
| Request ID | 用于客服查询与申诉 |
| 场景 | `capture/knowledge/rag/creation/image/task/code` |
| 品牌模型 | 如 MBCD Plus v1.0，不展示真实供应商 |
| Token/图片单位 | 输入、输出和其他计量单位 |
| Credit | 实际扣除、冻结中或已退回 |
| 延迟 | 首 token 与总耗时详情 |
| 状态 | 成功、用户取消、限流、余额不足、供应商错误 |

用户详情抽屉可展示计费公式、用量分解、退款原因与 trace 时间线，但默认不显示 prompt/回答正文。

### 4.2.6 云存储页

#### 页面结构

- 使用量卡片：已用/总额度、快照数、本月上下行流量。
- 快照列表：创建时间、来源设备、客户端版本、schema 版本、加密版本、大小、状态、保留策略。
- 操作：标记保留、删除、设为新设备恢复源；用户无需设置或复制恢复密钥。
- 上传状态：分块数、重试次数、最后错误；不展示 OSS Object Key、UploadId 或 STS 凭证。
- 恢复历史：目标设备、源快照、开始/完成时间、结果、兼容性迁移版本。

#### 用户文案边界

- 使用“加密备份与恢复”表述 MVP，避免使用“实时同步”。
- 明确说明恢复可能覆盖当前本地数据，客户端必须先自动创建回滚快照。
- 已登录账号默认拥有 50MB 空间；快照加密材料由后台托管并按快照临时下发，用户无需管理恢复密钥。

### 4.2.7 设备管理页

- 设备名称、平台、系统版本、客户端版本、最后在线、最近快照。
- 当前设备标识、重命名、吊销、强制退出。
- 新设备登录通知、异常地域登录通知。
- 设备加密公钥指纹，便于用户交叉核验。

### 4.2.8 账户与安全页

- 已验证手机号与邮箱：添加、更换、设为主联系方式，变更前验证旧身份与新身份。
- 第三方账号：微信、QQ 的绑定状态、授权时间、解绑和重新授权；解绑前必须保留至少一种可用登录方式。
- 账号合并：当用户拥有两个独立账号时，提供显式合并入口、资产预览与冲突提示。
- 邀请与奖励：查看自己的邀请码、已邀请状态、奖励进度、已发放的余额/Credit/套餐及规则说明。
- 密码与 Passkey/MFA、活跃会话、登录历史。
- 数据导出、删除云快照、注销账户。
- 隐私授权：云模型、云快照、诊断日志、个性化分析独立开关。

## 4.3 内部运营控制台

内部运营台不应与用户控制台共享普通用户会话。需求模块如下：

| 模块 | 能力 |
| --- | --- |
| 运营驾驶舱 | GMV、付费用户、Credit 消耗、供应商成本、毛利、网关成功率、快照成功率 |
| 用户管理 | 基本资料、套餐、设备、余额、风险标签、脱敏调用元数据 |
| 订单支付 | 订单、支付回调、退款、对账差异、发票 |
| Credit 账本 | 充值、赠送、冻结、扣减、退回、过期、手工调账与双人复核 |
| 套餐商品 | 月/年套餐、Credit 包、存储权益、生效时间、渠道与灰度 |
| 邀请活动 | 活动、邀请码批次、触发条件、双边奖励、余额/Credit/套餐发放、反作弊 |
| 短信运营 | 阿里云签名/模板配置引用、发送量、到达率、错误码、成本、异常号码/IP |
| 模型与路由 | 品牌模型、供应商 endpoint、模型映射、价格版本、权重、降级、灰度 |
| 网关观测 | QPS、并发、TTFT、耗时、错误、超时、成本与毛利，按路由版本对比 |
| 快照运营 | 仅查看密文大小、数量、版本、失败和存储成本，无明文下载入口 |
| 风控 | 盗刷、账户共享、支付异常、请求激增、高成本模型滥用 |
| 审计 | 管理员登录、查询、导出、调账、路由变更和密钥轮换记录 |

敏感操作规则：

- 供应商密钥只能“新建/替换/禁用”，不能回显明文。
- 手工 Credit 调账、大额退款、模型全量切换、邀请奖励规则发布必须有变更理由与审批记录。
- 邀请活动发布后创建不可变的规则版本；后续修改只影响新绑定，已满足条件的奖励不得被静默撤销。
- 运营导出文件添加操作人水印、短时效下载链接和导出审计。

---

## 5. `mb-gateway` 产品与技术设计

## 5.1 模型与协议边界

`Claude Code` 是模型调用客户端，不是真实的底层模型供应商。因此本方案将需求拆成两条产品能力：

1. **MemoryBread 客户端调用**：`MBCD Plus v1.0` 可路由到阿里云百炼 `qwen3.7-plus/max` 或符合策略的 Claude 模型。
2. **Claude Code 兼容接入**：`mb-gateway` 提供 Anthropic 兼容 endpoint，用户或受管环境可通过 `ANTHROPIC_BASE_URL` 将 Claude Code 指向网关。

二者可共用路由、计量和账本，但 `caller` 必须分别记录为 `creation/rag/...` 与 `code`，便于套餐限制、成本分析和风控。

## 5.2 对外 API 形态

### MemoryBread 原生入口

```http
POST /v1/gateway/chat
Authorization: Bearer <user_or_device_access_token>
Idempotency-Key: <uuid>
X-MB-Device-ID: <device_id>
```

```json
{
  "request_id": "01J...",
  "brand_model_id": "mbcd-plus-v1",
  "caller": "creation",
  "messages": [],
  "stream": true,
  "privacy": {
    "content_logging": false,
    "client_scrubbed": true
  },
  "limits": {
    "max_output_tokens": 4096,
    "max_credit": "25.0000"
  }
}
```

### OpenAI 兼容入口

```text
POST /openai/v1/chat/completions
GET  /openai/v1/models
```

### Anthropic/Claude Code 兼容入口

```text
POST /anthropic/v1/messages
POST /anthropic/v1/messages/count_tokens
```

兼容入口只保证经过验证的子集，需对 streaming、tool use、prompt caching、thinking blocks、token counting 和错误结构建立协议契约测试。不支持的字段应显式报错，不得静默丢弃。

## 5.3 品牌模型与路由

### 品牌模型

| 品牌模型 | 定位 | 默认路径 | 计费 |
| --- | --- | --- | --- |
| MBEM v1.0 | 本地提炼 | 客户端本地模型 | 不扣 Credit |
| MBCD Std v1.0 | 标准创作 | 本地模型，可选低成本远程降级 | 默认免费/套餐内 |
| MBCD Plus v1.0 | 高质量创作与咨询 | Qwen 3.7 / Claude 策略路由 | 扣 Credit |
| MBCD Code v1.0 | 代码任务 | 可验证的 code 模型路由 | 扣 Credit，可独立套餐 |
| GPT Image 2 | 图像生成 | 图像供应商 | 按张/质量扣 Credit |
| Gemini Nano Banana | 图像生成 | 图像供应商 | 按张/质量扣 Credit |

### 路由决策输入

- `brand_model_id` 与业务场景。
- 套餐、用户灰度组、地域和数据驻留策略。
- 上下文长度、tool use/图像/thinking 等能力需求。
- 供应商实时健康、限流、TTFT、错误率和剩余配额。
- 单次成本上限、目标毛利和路由价格版本。
- 会话粘性：同一会话优先保持模型与路由版本，避免风格和 tool behavior 突变。

### 路由策略

```text
能力硬过滤 -> 合规/地域过滤 -> 健康过滤 -> 灰度策略
    -> 粘性路由 -> 成本/质量/延迟加权 -> 主路由 + 有序降级链
```

降级只能发生在能力兼容的路由之间。请求已经向用户输出流式 token 后，不得自动切换供应商重放整个请求，否则会产生重复内容与双重计费。

## 5.4 供应商适配器

每个 adapter 实现统一接口：

```text
validate_config
list_capabilities
estimate_tokens
invoke
invoke_stream
normalize_usage
normalize_error
health_check
```

初期 adapter：

- `qwen_openai_compatible`：对接阿里云百炼 OpenAI 兼容 endpoint，endpoint、workspace、区域和模型名均为运营配置。
- `anthropic_messages`：对接 Anthropic Messages API 或经过验证的 Anthropic 兼容 endpoint。
- `openai_compatible`：通用兼容适配器，只用于通过契约测试的 endpoint。

供应商配置包含：`base_url`、加密密钥引用、区域、超时、代理、TLS 策略、并发上限、请求头模板和能力标签。禁止将密钥以明文放在数据库或管理页面。

## 5.5 计量与 Credit 结算

### 核心规则

- Credit 计算使用定点小数/Decimal，禁止浮点数。
- 价格、路由和汇率都是版本化的，请求开始时固化快照。
- 请求前根据上下文与 `max_output_tokens` 冻结上限，结束后按供应商用量或网关 tokenizer 结算，多退少补。
- 同一 `Idempotency-Key + account_id` 只允许一次账务结果。
- 供应商失败、网关失败、用户中断和部分输出必须分别配置计费策略。
- 账本只追加；任何冲正都使用新的反向分录。

### Credit 批次消耗顺序

1. 即将过期的赠送 Credit。
2. 即将过期的套餐 Credit。
3. 购买 Credit。
4. 其他无期限赠送 Credit。

消耗顺序应作为可版本化政策，并在用户端清晰说明。

### 结算状态机

```text
created -> reserved -> provider_started -> streaming -> settled
              |               |               |
              +-> rejected    +-> failed       +-> reversed/adjusted
```

## 5.6 限流、预算与风控

按以下维度组合限制：

- IP 的登录/注册/验证码频率。
- 账户、设备、套餐、品牌模型的 QPS 与并发。
- 每分钟 token、每日 Credit、单请求最大 Credit。
- 试用 Credit 按账户 + 设备指纹 + 支付身份的多维防刷。
- 突发高成本请求、异常多设备切换、重复退款等风险事件。

限流返回稳定错误码、`retry_after_ms` 和当前额度摘要，但不暴露全局容量。

## 5.7 流式调用可靠性

- 网关向客户端发送心跳，防止中间代理在长思考阶段关闭连接。
- 客户端断开后尽快取消上游；如上游不支持取消，仍要记录实际成本。
- 仅在没有产生任何下游输出时自动重试，重试次数受全链路 deadline 限制。
- 用量事件、账本与 outbox 在同一个阿里云 RDS PostgreSQL 事务中提交，保证最终投递，不依赖“请求结束后再异步尽力写入”。

---

## 6. 云快照与多设备迁移设计

## 6.1 快照内容

不建议在运行中直接打包 SQLite/WAL 与 Qdrant 数据目录。客户端应进入“快照一致性点”：

1. Rust core-engine 使用 SQLite Online Backup API 生成一致副本。
2. 记录当前 DB schema、客户端、模型和向量维度版本。
3. 原始截图、生成文档、模板等文件资产按用户策略包含。
4. Qdrant 向量索引默认不上传，恢复后基于 SQLite 元数据重建；这会增加恢复时间，但能减小快照、避免索引版本耦合。

建议包内结构：

```text
manifest.json
database/captures.sqlite
assets/screenshots/...       # 用户可选
assets/documents/...
preferences/export.json
checksums.json
```

## 6.2 Manifest

```json
{
  "snapshot_id": "01J...",
  "created_at": "2026-06-30T00:00:00Z",
  "source_device_id": "dev_...",
  "client_version": "0.2.0",
  "schema_version": 18,
  "snapshot_format_version": 1,
  "encryption_version": 2,
  "compression": "zstd",
  "content_policy": {
    "include_screenshots": false,
    "include_generated_documents": true,
    "include_diagnostics": false
  },
  "files": [],
  "logical_stats": {
    "captures": 0,
    "knowledge_entries": 0,
    "bake_documents": 0
  }
}
```

## 6.3 加密与密钥管理

- 每个快照使用服务端根密钥和 scoped object key 派生独立 Data Encryption Key（DEK），客户端使用 AEAD 加密。
- DEK 只在通过快照归属校验后的备份或恢复请求中临时下发，不写客户端普通配置或日志。
- 设备本地密钥放在 macOS Keychain/Windows Credential Manager，不写入普通配置文件。
- 服务端保存密文块和加密 manifest；根密钥仅通过运行时 Secret 注入，不进入数据库、日志或客户端包。
- 块标识使用用户级密钥 HMAC，避免公开内容哈希泄露跨用户的相同文件。

## 6.4 上传协议

1. `POST /v1/snapshots` 在 RDS 中创建上传会话、检查套餐额度，并向 OSS 初始化 Multipart Upload。
2. `POST /v1/snapshots/{id}/parts:prepare` 由服务端通过 RAM/STS 下发短时效、最小权限的 OSS 临时凭证；凭证只允许访问当前用户、当前快照的 Object Key 前缀。
3. 客户端使用 OSS Multipart Upload 并发直传密文分片，不经过 `mb-admin-api` 中转；本地记录 `UploadId` 、PartNumber 和 ETag，网络恢复后续传。
4. `POST /v1/snapshots/{id}:commit` 提交 manifest、分片 ETag 列表与密文总大小；服务端验证后调用 OSS CompleteMultipartUpload。
5. 后台通过 OSS 对象元数据与客户端密文校验信息检查完整性，再将 RDS 中的快照状态从 `uploading` 改为 `available`。
6. 长时间未提交的会话自动过期；worker 调用 OSS AbortMultipartUpload，并配置 OSS Lifecycle 作为孤儿分片的最终清理保障。

OSS Bucket 必须为私有读写、禁止公共 ACL，开启服务端加密作为客户端密文之外的第二层保护。生产快照、官网静态资源、账单/导出文件使用不同 Bucket 和不同 RAM Role，禁止共享写入权限。

## 6.5 恢复与冲突策略

MVP 采用“快照分支 + 显式恢复”，不做静默双向合并：

- 每个设备生成自己的快照序列，服务端不自动判断哪份内容是“真相”。
- 新设备首次启动时，用户主动选择恢复点。
- 有本地数据时，提供“保留本地”、“先备份再覆盖”；不提供未验证的自动合并。
- 恢复先在临时目录解密、校验和迁移，通过完整性检查后再原子切换数据目录。
- 切换失败时自动恢复本地回滚快照。

二期如要实现真正多端同步，应将 captures、knowledge、preferences 等核心变更抽象为可合并操作日志，对不同实体定义 LWW、集合合并或手工冲突规则，而不是合并 SQLite 文件。

## 6.6 保留与删除

- 套餐指定最大存储、快照数和保留天数。
- 用户标记“保留”的快照不受普通轮转策略影响，但仍占用额度。
- 删除请求先在 RDS 生成墓碑，worker 异步删除 OSS Object/Version，在管理台显示删除进度。
- 账户注销时停止新上传，吊销设备凭证，并在声明周期内删除快照密文和账户 PII；法定账务记录按法规保留。

---

## 7. 系统架构

## 7.1 逻辑架构

```text
                          +----------------------+
                          | Website / Console    |
                          | User / Internal Ops  |
                          +----------+-----------+
                                     |
MemoryBread Desktop ---- CDN/WAF ----+------ API Gateway
        |                                       |
        |                              +--------+---------+
        |                              | mb-admin-api     |
        |                              | modular monolith |
        |                              +--+-----+------+---+
        |                                 |     |      |
        |                              account billing snapshot
        |                                 |     |      |
        +---------------------> +----------+-----+------+---+
                                | Alibaba Cloud managed    |
                                | RDS PG / Tair / OSS      |
                                +----------+---------------+
                                           |
        +----------------------------------+----------------+
        | mb-gateway                                        |
        | auth -> quota -> reserve -> route -> adapter       |
        |      -> stream -> meter -> settle -> outbox        |
        +----------------------+-----------------------------+
                               |
                  +------------+-------------+
                  |                          |
             Alibaba Qwen             Anthropic/compatible
```

## 7.2 服务划分

### MVP：模块化单体 + 独立网关

| 组件 | 职责 | 建议形态 |
| --- | --- | --- |
| `mb-admin-web` | 官网、用户控制台、运营台 | React/TypeScript SSR/SSG Web 应用 |
| `mb-admin-api` | 账户/联合身份、邀请奖励、套餐、订单、Credit、快照、运营 API | Rust + axum 模块化单体 |
| `mb-gateway` | 低延迟流式网关、路由、计量、结算 | 独立 Rust + axum 进程 |
| `mb-worker` | outbox、支付对账、快照清理、聚合指标 | 与 admin-api 共库的独立进程 |

使用 Rust + axum 可延续当前 core-engine 的技术经验，同时适合长连接流式代理。如团队后续在业务后台上更熟悉 Go/Java，`mb-admin-api` 可独立评估，但不建议 MVP 同时引入多种后端语言。

### 后续拆分条件

仅在出现独立扩容、独立发布、故障隔离或团队所有权需求时拆分：

- 模型网关已经独立，因为其负载形态和发布风险与控制台不同。
- 快照控制面在大规模上传后可拆为 `mb-sync-control`，数据面仍使用 STS 临时凭证直传阿里云 OSS。
- 计费在出现多产品共享账本或更严格财务合规时拆为独立服务。

## 7.3 基础设施

| 能力 | MVP 建议 | 说明 |
| --- | --- | --- |
| 主数据库 | **阿里云 RDS PostgreSQL 高可用系列** | 账户、身份、邀请、订单、账本、用量、快照元数据和 outbox 的唯一事实库 |
| 缓存/限流 | **阿里云 Tair（Redis 兼容）** | 会话短期索引、分布式限流、验证码短期状态、路由健康；账本真相不放 Tair |
| 对象存储 | **阿里云 OSS** | 快照密文、账单/导出文件和官网静态资源使用独立私有/公开 Bucket |
| 密钥/凭证 | **阿里云 KMS + RAM + STS** | 供应商/支付/短信密钥、RDS/OSS 访问权限、客户端 OSS 临时凭证 |
| 异步任务 | RDS PostgreSQL outbox + `mb-worker` | 首期不引入 Kafka，worker 无本地持久化状态 |
| 边缘/接入 | 阿里云 CDN + WAF + ALB | 官网静态资源与 API/流式网关分开超时、缓存与防护策略 |
| 可观测 | OpenTelemetry + 阿里云 SLS/ARMS | 所有服务使用统一 `trace_id`，日志不落服务器本地盘 |

### 7.3.1 RDS PostgreSQL 配置基线

- 生产使用高可用系列与跨可用区主备，不使用 ECS 自建 PostgreSQL。
- RDS、Tair 和后端服务部署在同一地域/VPC，只使用内网 endpoint；RDS 与 Tair 不开放公网访问。
- 使用独立数据库账号区分 `admin-api`、`gateway`、`worker`、migration 和只读运营查询，禁止共享高权限主账号。
- 开启自动数据备份与日志备份，支持 PITR；生产发布前定期演练“恢复到新 RDS 实例 -> 校验 -> 切换”。
- 初期不使用 RDS 的 OSS 冷数据归档作为在线表的一部分；用量冷数据先通过可回放的导出任务归档到独立 OSS Bucket。

### 7.3.2 OSS 配置基线

- `mb-snapshots-{env}`：私有 Bucket，存储客户端加密快照，启用服务端加密、Lifecycle 和按需版本化。
- `mb-private-exports-{env}`：私有 Bucket，存储账单、数据导出和运营导出，使用短时效下载授权。
- `mb-web-assets-{env}`：官网公开资源 Bucket，仅经 CDN 对外，与私有数据 Bucket 完全隔离。
- 禁止客户端持有长期 AccessKey；上传/下载只使用 STS 最小权限临时凭证或同等短时效授权。
- OSS Object Key 由服务端生成，不包含邮箱、手机号、设备名或其他可识别信息。

## 7.4 阿里云部署拓扑

```text
Internet
  -> Alibaba Cloud CDN/WAF/ALB
  -> mb-admin-web / mb-admin-api / mb-gateway / mb-worker
       -> VPC private endpoint -> RDS PostgreSQL HA
       -> VPC private endpoint -> Tair
       -> RAM Role/STS         -> OSS
       -> KMS                  -> encrypted application secrets
       -> SLS/ARMS             -> logs, metrics and traces
```

应用可部署在 ECS、容器服务或函数形态，但应用节点都是可替换的无状态计算资源。唯一允许的本地文件是有大小/时间限制的临时文件，请求结束后立即删除，不作为任何业务数据的真相源。

## 7.5 仓库建议结构

```text
server/
  mb-admin-web/
    app/
      (marketing)/
      (console)/
      (ops)/
    components/
    lib/
  mb-admin-api/
    src/modules/
      identity/
      verification/
      invitation/
      notification/
      billing/
      subscription/
      usage/
      snapshot/
      device/
      operations/
  mb-gateway/
    src/
      auth/
      routing/
      providers/
      metering/
      settlement/
      protocol/
  mb-worker/
  crates/
    domain/
    database/
    observability/
    contracts/
  migrations/
  deploy/
```

---

## 8. 核心数据模型

所有主键建议使用 UUIDv7/ULID，金额与 Credit 使用 Decimal，时间在数据库中统一 UTC。

### 8.1 身份与设备

| 表 | 关键字段 |
| --- | --- |
| `users` | id, username, nickname, company_name, status, locale, timezone, primary_identity_id, created_at, deleted_at |
| `user_profile_field_changes` | id, user_id, field_name(`nickname/company_name`), changed_at |
| `identities` | id, user_id, type(`phone/email/oauth`), provider, provider_subject, normalized_identifier_hash, display_identifier_encrypted, verified_at, revoked_at |
| `oauth_authorizations` | provider, state_hash, pkce_challenge, redirect_target, expires_at, consumed_at |
| `account_merge_requests` | source_user_id, target_user_id, status, conflict_snapshot, verified_at, completed_at |
| `verification_challenges` | id, purpose, target_hash, code_hash, expires_at, attempts, send_status, consumed_at |
| `message_deliveries` | challenge_id, provider, template_code, provider_request_id, biz_id, status, error_code, sent_at, delivered_at |
| `devices` | id, user_id, name, platform, client_version, public_key, last_seen_at, revoked_at |
| `sessions` | id, user_id, device_id, refresh_token_hash, expires_at, revoked_at |
| `roles/user_roles` | RBAC 角色与赋权 |
| `admin_audit_logs` | actor, action, target_type/id, before_hash, after_hash, reason, ip, created_at |

`normalized_identifier_hash` 用于唯一索引与精确查找，并使用应用级密钥 HMAC，避免简单 SHA 哈希被枚举；用于展示和通知的手机号/邮箱单独加密存储。OAuth `provider_subject` 使用平台返回的稳定唯一标识，不使用昵称、头像等可变字段做账号键。

### 8.2 邀请与权益发放

| 表 | 关键字段 |
| --- | --- |
| `referral_campaigns` | name, status, starts_at, ends_at, audience, code_policy, risk_policy_version |
| `referral_rule_versions` | campaign_id, version, trigger, inviter_reward_json, invitee_reward_json, effective_from |
| `invite_codes` | campaign_id, owner_user_id, code_hash, code_display, max_uses, used_count, expires_at, status |
| `invite_bindings` | campaign_id, code_id, inviter_user_id, invitee_user_id, rule_version_id, bound_at, status, risk_state |
| `reward_grants` | binding_id, beneficiary_user_id, side, reward_type, reward_ref, amount, status, idempotency_key, granted_at |
| `entitlement_grants` | user_id, entitlement/product_id, source_type/id, starts_at, ends_at, status |
| `balance_accounts/ledger` | user_id, balance_type, available, ledger entry type/amount/source, expires_at |

其中 `reward_type` 支持 `promotional_balance | credit | subscription | entitlement | coupon`。月度/年度套餐奖励应发放一条期限明确的订阅/权益记录，不修改套餐商品定义。已经存在付费订阅时，奖励规则必须明确是顺延到期时间、下个周期生效、还是转为等值 Credit，禁止默认覆盖现有订阅。

### 8.3 商品、订阅和支付

| 表 | 关键字段 |
| --- | --- |
| `products` | type, name, status |
| `prices` | product_id, currency, amount, billing_period, effective_from/to |
| `entitlement_definitions` | code, value_type |
| `product_entitlements` | product_id, entitlement_code, value |
| `subscriptions` | user_id, price_id, status, current_period_start/end, cancel_at_period_end |
| `orders` | order_no, user_id, type, amount, currency, status, idempotency_key |
| `payments` | order_id, channel, provider_trade_no, amount, status, paid_at |
| `refunds` | payment_id, amount, reason, status, provider_refund_no |
| `payment_webhook_events` | provider_event_id, payload_hash, status, processed_at |

支付回调必须先落原始事件哈希和处理状态，然后幂等驱动订单状态，不以浏览器跳转结果作为支付成功依据。

### 8.4 Credit

| 表 | 关键字段 |
| --- | --- |
| `credit_accounts` | owner_type/id, status, available, reserved, version |
| `credit_grants` | account_id, source_type/id, original_amount, remaining_amount, expires_at, priority |
| `credit_reservations` | request_id, account_id, amount, status, expires_at |
| `credit_ledger` | account_id, request_id, event_id, grant_id, entry_type, amount, balance_after, created_at |
| `credit_adjustments` | reason, requested_by, approved_by, ledger_entry_id |

`credit_accounts.available/reserved` 是可高效查询的物化余额，`credit_ledger` 是审计真相。两者在同一数据库事务内变更，并使用账户版本/行锁防止超扣。

### 8.5 模型网关

| 表 | 关键字段 |
| --- | --- |
| `brand_models` | id, display_name, family, status, public_capabilities |
| `provider_endpoints` | provider, region, base_url, secret_ref, status |
| `provider_models` | endpoint_id, provider_model, capabilities, context_limit |
| `route_versions` | brand_model_id, version, status, effective_from/to |
| `route_targets` | route_version_id, provider_model_id, weight, priority, conditions_json |
| `price_versions` | provider_model_id, input/output/cache/image prices, currency, effective time |
| `credit_rate_versions` | brand_model_id, usage_unit, credit_rate, effective time |
| `usage_events` | request_id, trace_id, user/device, caller, brand model, route/price version, normalized usage, status |
| `provider_cost_events` | usage_event_id, provider units, cost, currency, estimated/final |
| `gateway_attempts` | usage_event_id, attempt, endpoint, started/finished, error, TTFT |

面向用户的查询视图隐藏 `provider_endpoints`、`provider_models`、`provider_cost_events` 和真实路由字段。

### 8.6 快照

| 表 | 关键字段 |
| --- | --- |
| `snapshots` | user_id, device_id, format/schema/encryption version, encrypted_size, status, retention, committed_at |
| `snapshot_parts` | snapshot_id, part_no, object_key, size, ciphertext_checksum, status |
| `snapshot_upload_sessions` | snapshot_id, upload_id, expires_at, status |
| `restore_jobs` | user_id, device_id, snapshot_id, status, client_report, started/completed_at |
| `storage_usage_daily` | user_id, date, bytes, snapshot_count, ingress, egress |
| `deletion_tombstones` | resource_type/id, requested_at, delete_after, completed_at |

---

## 9. API 清单

### 9.1 账户与设备

```text
POST   /v1/auth/phone/challenges        # 发送手机验证码
POST   /v1/auth/phone/verify            # 验证并注册/登录
POST   /v1/auth/email/challenges        # 发送邮箱验证码
POST   /v1/auth/email/verify
POST   /v1/auth/oauth/{provider}/start  # provider: wechat/qq
GET    /v1/auth/oauth/{provider}/callback
POST   /v1/auth/token/refresh
POST   /v1/auth/logout
GET    /v1/me
PUT    /v1/auth/profile                # 修改昵称与公司名称，按字段执行月度限次
GET    /v1/me/identities
POST   /v1/me/identities/phone
POST   /v1/me/identities/email
POST   /v1/me/identities/oauth/{provider}
DELETE /v1/me/identities/{id}
POST   /v1/account-merges
POST   /v1/account-merges/{id}:confirm
GET    /v1/devices
PATCH  /v1/devices/{id}
DELETE /v1/devices/{id}
```

发送验证码请求包含 `purpose`、人机验证 token（风控触发时）和客户端生成的幂等键；返回 `challenge_id`、`expires_in`、`retry_after`，不返回验证码或“该手机号已注册”等可枚举信息。

### 9.2 邀请与奖励

```text
GET  /v1/invitations/code
POST /v1/invitations/code:rotate
POST /v1/invitations/validate
POST /v1/invitations/bind
GET  /v1/invitations/referrals
GET  /v1/invitations/rewards
```

`POST /v1/invitations/bind` 需要已验证账户与幂等键，返回绑定状态和用户可见的奖励条件，不返回内部风控标记。奖励发放是触发事件驱动的独立业务结果，不得与邀请码绑定处理混成一个无法重试的跨系统事务。

### 9.3 钱包、套餐与订单

```text
GET  /v1/billing/balance
GET  /v1/billing/credit-grants
GET  /v1/billing/ledger
GET  /v1/catalog/products
POST /v1/orders
GET  /v1/orders/{id}
POST /v1/orders/{id}:pay
POST /v1/subscriptions
PATCH /v1/subscriptions/{id}
GET  /v1/invoices
```

### 9.4 用量

```text
GET /v1/usage/summary?from=&to=&group_by=
GET /v1/usage/timeseries?metric=&interval=&from=&to=
GET /v1/usage/events?cursor=&caller=&brand_model_id=&status=
GET /v1/usage/events/{request_id}
GET /v1/usage/export
```

### 9.5 快照

```text
POST   /v1/snapshots
GET    /v1/snapshots
GET    /v1/snapshots/{id}
POST   /v1/snapshots/{id}/parts:prepare
POST   /v1/snapshots/{id}:commit
POST   /v1/snapshots/{id}:abort
POST   /v1/snapshots/{id}:download
DELETE /v1/snapshots/{id}
POST   /v1/restores
PATCH  /v1/restores/{id}
```

### 9.6 运营 API

运营 API 使用独立 `/ops/v1` 前缀、独立身份提供商、MFA 与 IP/设备策略。所有列表查询强制分页、导出异步执行，所有变更要求理由并生成审计事件。

---

## 10. 安全、隐私与合规

## 10.1 威胁与对策

| 威胁 | 主要对策 |
| --- | --- |
| 供应商 API Key 泄露 | KMS/Secret Manager、不回显、最小权限、定期轮换、出站域名白名单 |
| 用户 token 被盗 | 短 access token、refresh token 轮换与重用检测、设备吊销 |
| 短信轰炸/验证码爆破 | 手机/IP/设备/ASN 多维限流、人机验证、发送冷却、尝试上限、异常告警 |
| 账号枚举 | 注册/登录/找回接口使用统一响应、时序差异控制、脱敏日志 |
| OAuth 登录劫持 | state + PKCE + nonce、固定 redirect URI、一次性 authorization record、绑定前二次验证 |
| 账号错误合并 | 双账号控制权验证、资产预览、订阅冲突人工处理、合并审计与回滚窗口 |
| 邀请奖励羊毛党 | 身份/设备/支付工具/网络图谱去重、延迟发奖、风险冻结、反向账本冲正 |
| Credit 重复扣减 | 幂等键、唯一约束、同事务账本、对账任务 |
| 余额并发超扣 | 账户行锁/乐观版本、冻结后调用 |
| 快照泄露 | 客户端 AEAD 加密、STS 最小权限临时凭证、OSS 私有 Bucket、服务端加密、没有运营明文下载 |
| 上传资源耗尽 | 创建会话前配额检查、分块上限、总量上限、过期清理 |
| SSRF/恶意 endpoint | 运营级 endpoint 白名单、DNS/IP 检查、出站网络策略、禁止用户任意 URL |
| 运营越权 | 独立身份、RBAC/ABAC、MFA、最小权限、导出审计、敏感操作复核 |
| Prompt 在日志中泄露 | 结构化日志 allowlist，禁止记录 request body/SSE chunk，错误信息脱敏 |

## 10.2 数据分类

| 等级 | 例子 | 要求 |
| --- | --- | --- |
| 公开 | 官网文案、公开模型名 | CDN 可缓存 |
| 内部 | 聚合运营指标、路由健康 | 内部 RBAC |
| 机密 | 手机号、邮箱、第三方身份、订单、邀请关系、用量明细、设备信息 | 传输/存储加密、脱敏展示、严格导出控制 |
| 严格机密 | 短信/OAuth/供应商/支付密钥、验证码哈希、快照密文、用户内容 | KMS、特权隔离、默认不记录/不解密 |

## 10.3 隐私产品要求

- 首次使用云模型时告知哪些数据会发往云端及供应商类型。
- 首次开启快照时说明加密方式、50MB 默认额度、包含范围与删除策略。
- 云模型与云快照的授权独立，不通过绑定同意强制开启。
- 诊断采样必须显式、限时、可撤销，默认仅采集元数据。
- 账户注销、数据导出和云快照删除都要有可见状态与完成通知。

---

## 11. 可观测性与 SLO

### 11.1 统一观测字段

`trace_id`、`request_id`、`user_id_hash`、`device_id_hash`、`brand_model_id`、`route_version`、`provider_endpoint_id`、`usage_event_id`、`order_id`、`snapshot_id`。

### 11.2 核心指标

#### 网关

- 请求数、并发、成功率、限流率、超时率。
- TTFT P50/P95/P99、总耗时、上游连接耗时。
- 输入/输出/cache token、Credit、供应商成本、毛利率。
- 按品牌模型、真实路由、路由版本和场景分组。

#### 计费

- 冻结未结算数量与年龄、账本写入失败、余额对账差异。
- 订单支付成功率、回调延迟、重复回调、日对账差异。

#### 身份、短信与邀请

- 按渠道的注册/登录成功率、OAuth callback 错误、账号合并冲突和异常登录。
- 阿里云短信提交成功率、运营商到达率、P95 到达耗时、单次成功验证成本、发送限流与爆破拦截量。
- 邀请码曝光/绑定/激活转化、双边奖励成本、风险冻结率、发放失败与重试年龄。

#### 快照

- 上传/提交/恢复成功率、平均快照大小、分片重试、孤儿分片。
- 按客户端版本和 schema 版本的失败分布。

### 11.3 初期 SLO

| 服务 | 指标 | 目标 |
| --- | --- | --- |
| 账户/账单 API | 月可用性 | 99.9% |
| 模型网关自身 | 非供应商原因成功率 | 99.9% |
| 网关附加 TTFT | P95 | < 150ms（不含供应商） |
| Credit 账务 | 重复扣费 | 0 |
| 身份绑定 | 同一第三方 subject 绑定多账户 | 0 |
| 邀请奖励 | 重复发放 | 0 |
| 快照提交 | 月成功率 | 99.5% |
| 运营敏感变更 | 审计覆盖 | 100% |

供应商失败和 MemoryBread 网关自身失败必须分开统计，但用户感知的端到端成功率也必须单独展示。

---

## 12. 部署、环境与发布

### 12.1 环境

- `development`：独立的阿里云开发 RDS PostgreSQL、Tair、OSS Bucket 和 KMS/RAM 身份；本地只运行应用代码与模拟支付/模型供应商，不启动本地数据库、Redis 或 MinIO。
- `staging`：独立阿里云账号/资源组，使用独立 RDS 实例、Tair 实例、OSS Bucket、密钥和供应商测试额度。
- `production`：生产阿里云账号/资源组，禁止开发人员直接连接 RDS 写入或使用 OSS 长期 AccessKey。

环境之间不共享 RDS 实例、Tair 实例、OSS Bucket、RAM Role 或 KMS 密钥。开发环境也必须使用虚构/脱敏数据，不从生产 RDS 直接复制用户资料。

### 12.2 发布策略

- 数据库迁移使用 expand -> deploy -> contract，保证新旧客户端并存。
- 品牌模型路由是数据配置版本，不与应用发布强绑定。
- 新路由先 shadow 计量/内部账户，再 1% -> 10% -> 50% -> 100% 灰度。
- 每次路由变更保留一键回滚到上一 `route_version` 的能力。
- 客户端快照格式使用可向前读取的版本化 manifest，服务端不以客户端版本字符串猜测格式。

---

## 13. 测试与验收

## 13.1 测试层次

| 层次 | 重点 |
| --- | --- |
| 单元测试 | Credit 精度、批次消耗、路由过滤、价格版本、快照 manifest |
| 属性测试 | 账本总额守恒、任意重试不重复扣费/发奖、余额不为负、一个新用户只绑定一个首次邀请 |
| 契约测试 | Qwen/OpenAI 兼容协议、Anthropic/Claude Code 兼容子集、阿里云 SendSms/回执、微信/QQ OAuth |
| 集成测试 | 手机/邮箱注册、OAuth 绑定、账号合并、邀请发奖、支付回调、上游超时、SSE 中断、冻结/结算、分片续传 |
| 安全测试 | 鉴权绕过、账号枚举、验证码爆破、OAuth CSRF、水平越权、SSRF、签名 URL、日志泄露、管理员权限 |
| 灾难演练 | 供应商不可用、Tair 故障、RDS PostgreSQL 主备切换/PITR、OSS 分片上传与访问授权部分失败 |
| 可用性测试 | 官网键盘/读屏、控制台图表、快照覆盖确认、账单可解释性 |

## 13.2 关键验收用例

1. 同一请求重试 10 次，只生成一个结算结果。
2. 100 个并发请求争用最后余额时，不出现负余额。
3. 供应商在输出前超时时可进入降级路由；输出后断开不重放内容。
4. 网关成功返回但 worker 暂时停机时，outbox 恢复后能完成计量投递且不重复。
5. 10GB 快照在上传 60% 时断网，重启客户端后从已完成分片续传。
6. 快照被篡改一个字节时，客户端在切换本地数据前拒绝恢复。
7. 新版 schema 快照在旧客户端恢复时明确拦截，不破坏当前本地数据。
8. 运营、客服、财务和网关管理员相互不能访问越权功能。
9. 日志、trace 和错误上报中不出现 prompt、回答、API Key 或快照解密密钥。
10. 用户能从任一 Credit 变动追溯到订单或调用，客服能使用 Request ID 解释账单。
11. 同一手机验证码并发提交 20 次，最多一次成功消费，且响应不泄露账号是否存在。
12. 阿里云 SendSms 调用超时时，同一 challenge 不因自动重试向用户发送多条短信。
13. 已属于账户 A 的微信/QQ 身份无法直接绑定账户 B，双重验证合并后所有资产归属与审计记录完整。
14. 同一邀请触发事件重放 100 次，邀请人与被邀请人的余额/Credit/套餐奖励均只发放一次。
15. 被风控冻结的奖励不进入可用余额；复核通过可发放，驳回则保留完整原因和账本记录。
16. RDS PostgreSQL 触发主备切换时，应用连接池能恢复，不产生重复订单、重复扣费或重复奖励。
17. 客户端获取的 OSS STS 凭证不能列出 Bucket、读取其他用户 Object、写入当前快照前缀以外的 Object，过期后立即失效。
18. 在一台应用节点本地盘为空的情况下重启/替换节点，账户、账本、路由和快照状态不丢失，证明服务端无本地持久化依赖。

---

## 14. 分阶段实施路线

## Phase 0：基础契约与风险验证（2–3 周）

- 固化品牌模型 ID、调用场景、用量字段和错误码。
- 创建阿里云 development/staging 的 RDS PostgreSQL、Tair、OSS、KMS/RAM/STS 基线，验证全链路内网访问与无本地持久化部署。
- 用实验性 adapter 验证 Qwen 3.7 与 Anthropic/Claude Code 兼容子集。
- 完成手机号/邮箱/微信/QQ 统一身份模型与账号合并规则，评审微信开放平台、QQ 互联与阿里云短信资质前置条件。
- 验证邀请码 -> 活动规则 -> 幂等发放余额/Credit/套餐的通用权益链路。
- 确定支付通道、商品模式、Credit 有效期和退款规则。
- 完成快照 PoC：SQLite 一致备份、加密、分片续传、恢复与回滚。

**出口条件**：协议、账本、快照三个最高风险点均有可运行 PoC 和测试结论。

## Phase 1：MVP 内测（6–8 周）

- 手机号/邮箱验证码注册登录、微信/QQ 绑定、设备绑定和用户控制台基础框架。
- 阿里云短信 SendSms、回执、本地幂等、多维限流与发送监控。
- 邀请码绑定、注册完成触发、Credit/促销余额发放和运营查询。
- `mb-gateway` 原生调用、Qwen 单路由、用量记录，先“影子计费”不实际扣款。
- 钱包余额、Credit 账本、充值订单沙箱。
- 用量趋势与调用流水。
- 手动创建快照、上传、列表、下载和恢复。
- 内部运营台的用户、用量、路由和审计基础页。

**出口条件**：内部账户连续运行两周，用量与供应商账单误差在确认阈值内，快照恢复无数据破坏。

## Phase 2：付费公测（4–6 周）

- 正式支付、充值 Credit、月/年套餐、退款与日对账。
- 邀请奖励扩展到首次付费、月/年套餐、双边奖励与风险冻结/冲正。
- 启用冻结/结算/退回，小范围真实扣 Credit。
- MBCD Plus 主路由 + 同能力降级路由，路由灰度与一键回滚。
- 定时自动快照、保留策略、额度通知和设备吊销。
- 官网、定价、下载、安全页和五个产品演示资产。

**出口条件**：支付日对账无未解释差异，重复扣费为 0，端到端成功率与毛利达到内部红线。

## Phase 3：规模化（6–12 周）

- Anthropic/Claude Code 兼容入口、API token 与独立 code 额度。
- 多区域供应商路由、质量/成本实验和路由自动摘除。
- 快照增量块复用、托管加密材料轮换与灾备恢复。
- 发票、企业账户、成员与团队额度（视市场需求）。
- 根据负载和团队边界选择拆分快照或计费服务。

---

## 15. 产品与经营指标

### 漏斗

- 官网访问 -> 下载点击 -> 安装 -> 完成引导 -> 7 日留存。
- 按手机号、邮箱、微信、QQ 分组的注册开始 -> 验证成功 -> 首次客户端登录 -> 7 日留存。
- 邀请链接打开 -> 邀请码绑定 -> 有效激活 -> 奖励发放 -> 被邀请用户付费/留存。
- 本地用户 -> 首次云模型 -> 首次充值 -> 订阅 -> 续费。
- 开启云快照 -> 首次成功快照 -> 第二设备恢复 -> 保持开启。

### 价值指标

- 周活跃记忆查询用户、周活跃文档创作用户。
- 引用历史上下文的创作占比。
- 首次价值时间：安装到首次成功历史回忆/文档生成。
- 快照成功率、恢复成功率与恢复后 7 日留存。

### 经营与质量指标

- ARPPU、付费转化、月/年付占比、续费率、退款率。
- 每 Credit 收入、每 Credit 供应商成本、毛利率。
- 按路由版本的成功率、TTFT、用户取消率与重试率。
- 账单申诉率、自动退回率、人工调账率。

---

## 16. 主要风险与决策

| 风险 | 建议决策 |
| --- | --- |
| “云同步”预期高于 MVP 能力 | 产品名称使用“加密快照/迁移”，不宣传实时同步 |
| 快照覆盖导致数据丢失 | 恢复前本地回滚点 + 临时目录校验 + 原子切换 |
| 端到端加密导致密钥丢失无法恢复 | 强引导保存恢复码；托管恢复作为独立选项与风险模型 |
| 模型路由切换引起质量波动 | 会话粘性、路由版本、内部评测、灰度和一键回滚 |
| 供应商价格变动导致毛利突降 | 价格版本、毛利告警、单次成本上限、路由政策及时生效 |
| 流式中断引起账单争议 | 预冻结、标准化部分用量、可解释账单、策略化退款 |
| Claude Code 协议演进 | 明确兼容子集、自动契约测试、按客户端版本灰度 |
| 短信签名/模板/实名报备延误上线 | Phase 0 即申请资质、签名和模板，邮箱登录作为可用降级通道 |
| 邀请活动被批量套利 | 延迟奖励触发、多维关联去重、风险冻结、不可变账本与冲正流程 |
| 第三方账号与已有手机/邮箱账号重复 | 统一 identity 模型、显式合并、双账号验证和资产冲突预览 |
| 个人开发者运维负担过高 | 模块化单体、阿里云 RDS PostgreSQL/Tair/OSS 托管服务、outbox 代替消息集群 |
| 资金钱包带来预付款合规复杂度 | MVP 支付后直接购买商品/Credit，不开放可提现或通用资金余额 |

---

## 17. 待产品/商业确认的决策

这些项不影响 Phase 0 技术 PoC，但必须在 Phase 1 结束前定案：

1. Credit 与人民币的展示关系、有效期、退款和套餐结转规则。
2. Free/Plus/Pro 的价格、Credit、云存储、设备数和并发限制。
3. 是否首发微信/支付宝，是否需要 App Store/Windows 渠道购买。
4. 云快照默认是否包含截图，建议默认不包含。
5. 平台托管快照加密材料已确定为首期方案，需完成根密钥轮换与灾备流程。
6. MBCD Plus v1.0 的质量基准、可接受的路由差异和目标毛利。
7. Claude Code 兼容入口是面向普通订阅用户、独立 Code 套餐，还是仅面向企业用户。
8. 中国大陆与海外用户的数据驻留、供应商与支付体系是否分区。
9. 手机号登录是否仅首发中国大陆 `+86`，以及国际/港澳台短信的上线时间。
10. 微信开放平台与 QQ 互联的主体资质、应用审核和网页/桌面端 redirect 方案。
11. 邀请码是全量用户一人一码、运营渠道码还是两者兼有，以及是否允许注册后限时补绑。
12. 邀请赠送“余额”的法律/财务定义：建议定义为不可提现、有适用范围的促销余额，与用户实际充值余额分账户。

---

## 18. 外部接口依据

- 阿里云百炼官方模型列表已列出 `qwen3.7-max` 与 `qwen3.7-plus`：<https://help.aliyun.com/zh/model-studio/models>
- 阿里云百炼支持 OpenAI-compatible 与 DashScope HTTP 调用，不同区域/workspace 使用对应 endpoint：<https://help.aliyun.com/en/model-studio/first-api-call-to-qwen>
- Anthropic 官方 Claude Code LLM Gateway 文档支持通过 `ANTHROPIC_BASE_URL` 指向 Anthropic 格式统一 endpoint：<https://docs.anthropic.com/en/docs/claude-code/llm-gateway>
- 阿里云短信服务可通过 SDK/API 调用 `SendSms`，中国站 endpoint 为 `dysmsapi.aliyuncs.com`，生产使用前需准备资质、已审核签名与模板：<https://help.aliyun.com/zh/sms/getting-started/use-sms-api/>
- 阿里云 `SendSms` 官方说明不提供幂等能力，且验证码短信建议单条发送，因此 MemoryBread 需在业务层建立 challenge 幂等与重试保护：<https://help.aliyun.com/zh/sms/developer-reference/api-dysmsapi-2017-05-25-sendsms>
- 阿里云 RDS PostgreSQL 高可用系列提供主备架构和故障切换，可将主备部署在不同可用区：<https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/rds-high-availability-edition>
- 阿里云 RDS PostgreSQL 支持自动数据备份与日志备份，开启日志备份后可用于时间点恢复：<https://help.aliyun.com/zh/rds/apsaradb-rds-for-postgresql/back-up-an-apsaradb-rds-for-postgresql-instance>
- 阿里云 OSS Multipart Upload 支持大文件分片并发与断点续传：<https://help.aliyun.com/zh/oss/user-guide/multipart-upload/>
- 阿里云 STS 可为客户端生成有效期内、受权限策略约束的 OSS 临时凭证：<https://help.aliyun.com/zh/oss/developer-reference/use-temporary-access-credentials-provided-by-sts-to-access-oss>

供应商模型名、价格、上下文限制和协议能力会变化，实施时不得固化在客户端或代码常量中，应由版本化的供应商配置和契约测试管理。

---

## 19. 结论

MemoryBread 服务端的最佳形态不是将当前本地应用重写成 SaaS，而是建立一个可选、可解释、可恢复的云能力层：

- 官网负责讲清“长期记忆 + 本地优先”的独特价值。
- 用户控制台负责让付费、用量、设备和快照完全可见。
- `mb-gateway` 负责隔离品牌模型与真实供应商，把成本、稳定性和账务变成可运营的能力。
- 云快照负责迁移和灾备，同时保持服务端对用户内容“不必看见、也不能看见”。

实施时应首先验证账本守恒、流式协议和快照恢复这三个高风险闭环，再扩展页面与运营能力。
