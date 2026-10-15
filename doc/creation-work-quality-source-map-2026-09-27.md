# 创作质量设计：来源与主张映射

核验日期：2026-09-27。读者：MemoryBread 产品、研发与内容评审人员。用途：支撑《如何提升创作质量可落地性》的理论说明及实施验收；这份映射是研究工作底稿，不作为运行验收结果。

## 来源登记

| ID | 来源、时间与类型 | 已核验范围 | 证据边界 |
| --- | --- | --- | --- |
| S1 | [Atlassian Project Poster](https://www.atlassian.com/team-playbook/plays/project-poster)，页面未明确标注发布日期；企业官方实践指南 | 原页的 Problem space、Validation、Ready to make it，以及更新与反馈要求 | 指南不等于针对该模板的随机试验，更不等于 MemoryBread 产品效果证据 |
| S2 | [GOV.UK content principles: conventions and research background](https://www.gov.uk/government/publications/govuk-content-principles-conventions-and-research-background/govuk-content-principles-conventions-and-research-background)，2013-06-10；政府研究背景综述 | 原页的阅读目的、清晰表达与语气区别、研究限制、参考文献 | 英文政务内容背景较早；体例惯例与理解表现要区分 |
| S3 | Mitroff、Featheringham，[On systemic problem solving and the error of the third kind](https://onlinelibrary.wiley.com/doi/abs/10.1002/bs.3830190605)，1974-11；理论论文 | 出版方摘要与书目信息；未读取付费全文 | 支持问题定义的概念依据，不支持效果数值或因果估计 |
| S4 | Shore，[Systematic Biases and Culture in Project Failures](https://journals.sagepub.com/doi/10.1002/pmj.20082)，2008；项目失败案例分析 | 出版方检索结果中的完整摘要与书目信息；全文受限 | 摘要描述八个案例；不能由此声称假设验证必然提高成功率 |
| S5 | Spyridakis，[Guidelines for Authoring Comprehensible Web Pages and Evaluating Their Success](https://www.researchgate.net/publication/230786762_Guidelines_for_Authoring_Comprehensible_Web_Pages_and_Evaluating_Their_Success)，2000，Technical Communication 47(3):359–382；文献综合与写作启发式 | 作者上传的原论文，重点读取阅读目的、先验知识、语气、可理解性及混杂变量讨论 | 作者上传页明确标注上传者；不是引用第三方概述。大学个人页的部分页码与期刊记录不一致，采用原论文印刷页码与期刊目录，未用大学页码 |
| S6 | Waller，[What makes a good document? The criteria we use](https://uploads-ssl.webflow.com/5c06fb475dbf1265069aba1e/6090fb24421b454c776fd1a8_SC2CriteriaGoodDoc_v5.pdf)，2011-04；Simplification Centre 技术报告 | 官方出版目录核对作者/时间，读取原 PDF 的四类评价标准、可读性与公式局限 | 专家评审框架结合研究与经验；不是通用于中文方案的已验证量表。现出版目录链接失效，引用仍可读取的官方历史 PDF |
| S7 | Pernice，[F-Shaped Pattern of Reading on the Web: Misunderstood, But Still Relevant](https://www.nngroup.com/articles/f-shaped-pattern-reading-web-content/)，2017-11-12，页面复核日期 2026-08-19；研究机构第一方眼动研究说明 | 原页对扫描模式多样性、F 型模式适用范围与设计的说明 | 不是所有人、所有文档都按 F 型阅读；不能直接外推中文内部方案 |
| S8 | 本地 [脑暴契约](../shared/creation-brainstorm/README.md)、[交付契约](../shared/creation-delivery/README.md)、[产品 README](../README.md)、mb-all/PROJECT_CONTEXT.md；2026-09-27 核验的项目资料 | 现有脑暴、确认来源、权限、本地优先与交付证据要求 | 契约和源码能支持设计边界，不能充当当前运行或生成质量实证 |

## 主张映射

| 主张 | 类型 | 依据 | 置信与未解问题 | 错误后果 |
| --- | --- | --- | --- | --- |
| C1：规划需区分问题、验证、实施准备 | 来源事实 | S1 | 高；实践框架不量化成效 | 将模板包装成科学验证结论 |
| C2：解决错问题属于不同于解题错误的概念风险 | 来源事实 | S3 | 高；仅使用摘要层概念 | 强加不必要流程 |
| C3：项目判断需注意偏差与文化影响 | 来源事实 | S4 | 中高；八案例的全文方法未核验 | 误述因果或泛化失败率 |
| C4：清晰表达需同时考虑目的、读者与语气 | 来源事实 | S2、S5 | 高；中文内部场景迁移尚待验证 | 将专业文档改成不合场景的口语 |
| C5：评价不能只看词句，还要看功能与行动 | 来源事实 | S6 | 高；框架适配方式由产品另行提出 | 忽视内容或夸大量表效度 |
| C6：阅读扫描模式不是单一 F 型 | 来源事实 | S7 | 高；具体中文任务须实测 | 错把网页经验当通用定律 |
| C7：六项机制可在现有创作链路中增量接入 | 产品建议 | S8，加本轮用户反馈 | 中；实施与真实回归进行中 | 文档宣传尚未接入的能力 |
| C8：盲评与任务验证能检验增量是否有效 | 验证建议 | 本轮产品设计 | 方法建议；样本量和正式门槛尚待按实测确定 | 仅凭自动评分宣布提升 |

## 不确定与排除

研究范围是工作方案与汇报的内容设计、技术传播及评价机制，不评价某个模型品牌。没有加入无法精确追溯的学者名言，没有采用他人场景中的效果百分比，没有宣称本产品通过了这些研究的实证检验。

来源派生内容预算按同一页面合并计算。本底稿只记录研究定位，发布正文仅保留每源少量必要主张，产品机制、例句、指标和验收步骤均明确为本轮推导。未经读取的付费全文不作为依据。

## 质量审阅

正文采用“研究综合 + 产品建议”结构，首段结论，重要事实附近连接来源。人工审阅按技能的决策用途、可追溯性、认识边界、综合性、压缩程度、复用性六项检查；脚本仅检查结构与引用，不替代来源核验。状态：设计/实施中，待真实回归。
