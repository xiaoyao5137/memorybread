import pytest

from creation.service import CreationService


def test_creation_skill_fallback_extracts_markdown_style_and_workflow():
    result = CreationService._fallback_creation_skill_analysis(
        "数据平台技术架构设计",
        "# 背景与目标\n内容说明。\n## 总体架构\n架构说明。\n## 实施计划\n计划说明。",
        "技术架构设计文档",
    )

    assert result["title"] == "技术架构设计文档"
    assert "structure_pattern" not in result
    assert result["common_titles"]
    assert len(result["diagram_style"]) >= 400
    assert len("".join(result["writing_guidelines"])) >= 400
    assert result["section_headings"]["common_titles"] == "标题设计风格"
    assert result["section_headings"]["text_style"] == "行文设计思路"
    assert result["section_headings"]["diagram_style"] == "图片生成方式"
    assert result["section_headings"]["writing_guidelines"] == "话术表达风格"
    assert result["title_style"] == "；".join(result["common_titles"])
    assert all(result["field_examples"].values())
    assert "数据平台" not in result["example_document"]
    assert result["skill_description"]["document_types"] == ["技术架构设计文档"]
    assert "软件与技术" in result["skill_description"]["domains"]
    assert [step["id"] for step in result["execution_steps"]] == [
        "collect-context",
        "analyze-data",
        "design-solution",
        "draft-document",
        "review-delivery",
    ]
    assert all(
        marker not in str(step.get("output") or "")
        for step in result["execution_steps"]
        for marker in ("证据不足", "证据缺口", "证据完备", "待核验")
    )
    assert result["execution_steps"][2]["agents"] == ["solution_design_agent"]
    assert result["execution_steps"][2]["tools"] == ["plantuml_diagram"]


def test_creation_skill_normalizer_fills_missing_model_fields():
    result = CreationService._normalize_creation_skill_analysis(
        {"title": "架构写作 Skill", "common_titles": ["A", "B"]},
        "原始架构文档",
        "# 背景\n正文内容足够长，用于测试缺失字段回退。",
        "架构设计文档",
    )

    assert result["title"] == "技术架构设计文档"
    assert result["common_titles"][:2] == ["A", "B"]
    assert len(result["common_titles"]) >= 4
    assert len(result["text_style"]) >= 400
    assert len(result["diagram_style"]) >= 400
    assert len("".join(result["writing_guidelines"])) >= 400
    assert "structure_pattern" not in result
    assert len(result["example_document"]) >= 1000
    assert result["skill_description"]["purpose"]
    assert result["skill_description"]["problems"]
    assert result["skill_description"]["deliverables"]
    assert result["execution_steps"][-2]["agents"] == ["document_writer_agent"]
    assert result["execution_steps"][-1]["agents"] == ["quality_review_agent"]


def test_creation_skill_normalizer_caps_each_step_agent_and_tool_resources():
    fallback = CreationService._fallback_skill_execution_steps(
        "行业研究报告",
        "行业研究报告",
        "示例行业研究",
        "需要调研、分析并形成方案。",
    )
    result = CreationService._normalize_skill_execution_steps(
        [
            {
                "id": "research",
                "title": "开展调研",
                "objective": "收集并分析证据。",
                "output": "带来源的结论",
                "agents": [
                    "industry_research_agent",
                    "data_analysis_agent",
                    "solution_design_agent",
                    "document_writer_agent",
                    "quality_review_agent",
                ],
                "skills": [],
                "tools": [
                    "memory_search",
                    "internet_search",
                    "github_search",
                    "plantuml_diagram",
                ],
            }
        ],
        fallback,
        "示例行业研究",
        "需要调研、分析并形成方案。",
    )

    assert result[0]["tools"] == ["memory_search", "internet_search"]
    assert result[0]["agents"] == [
        "industry_research_agent",
        "data_analysis_agent",
    ]
    assert len(result[0]["tools"]) + len(result[0]["agents"]) == 4


def test_creation_skill_prompt_requires_source_specific_style_fingerprint():
    prompt = CreationService._build_creation_skill_analysis_prompt(
        "示例方案",
        "# 为什么需要调整\n正文。\n## 方案如何落到执行\n正文。",
        "实施方案",
    )

    assert "风格指纹" in prompt
    assert "只替换其中可能泄密的主语、宾语" in prompt
    assert '"common_titles": "标题设计风格"' in prompt
    assert '"text_style": "行文设计思路"' in prompt
    assert '"diagram_style": "图片生成方式"' in prompt
    assert '"writing_guidelines": "话术表达风格"' in prompt
    assert "PlantUML" in prompt
    assert "一千二百至两千二百个中文字符" in prompt
    assert "目标对象 目标对象" in prompt
    assert "四百至七百个中文字符" in prompt
    assert "合计四百至七百个中文字符" in prompt
    assert '"structure_pattern"' not in prompt
    assert "只有 distinctive_sections 是对象数组" in prompt
    assert '"distinctive_sections": [' in prompt
    assert '"skill_description": {' in prompt
    assert '"execution_steps": [' in prompt
    assert "给创作 Agent 做触发判断" in prompt
    assert "industry_research_agent" in prompt
    assert "memory_search" in prompt


def test_creation_skill_fallback_preserves_heading_flow_voice_and_diagram_evidence():
    result = CreationService._fallback_creation_skill_analysis(
        "星火项目组迁移方案",
        """# 为什么需要调整

需要说明的是，现有边界需要重新确认。

## 方案如何落到执行

基于此，相关角色需要按步骤推进。

```plantuml
@startuml
component Example
@enduml
```

## 风险与后续

因此，需要明确复核方式。
""",
        "实施方案",
    )

    assert any("问句骨架" in item for item in result["common_titles"])
    assert "为什么需要调整" in result["field_examples"]["common_titles"]
    assert "方案如何落到执行" in result["field_examples"]["common_titles"]
    assert "需要说明的是" in str(result["writing_guidelines"])
    assert result["diagram_style"].startswith("证据与启用条件：")
    assert "PlantUML" in result["diagram_style"]
    assert len(result["diagram_style"]) >= 400
    assert len("".join(result["writing_guidelines"])) >= 400
    assert "星火项目组" not in str(result)


def test_creation_skill_payload_constrains_complete_json_in_response():
    payload = CreationService._creation_skill_analysis_payload("local-model", "prompt")

    assert payload["format"]["type"] == "object"
    assert payload["format"]["additionalProperties"] is False
    assert set(payload["format"]["required"]) >= {
        "skill_description",
        "execution_steps",
        "section_headings",
        "field_examples",
        "example_document",
    }
    assert payload["format"]["properties"]["execution_steps"]["items"]["required"] == [
        "id",
        "title",
        "objective",
        "output",
        "agents",
        "skills",
        "tools",
    ]
    assert payload["stream"] is False
    assert payload["think"] is False
    assert payload["options"]["num_predict"] == 8192


@pytest.mark.asyncio
async def test_creation_skill_marks_invalid_model_json_without_claiming_service_outage(
    monkeypatch,
):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": '{"title":"流程说明","summary":"未闭合"'}

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "creation.service.httpx.AsyncClient",
        lambda **_kwargs: FakeAsyncClient(),
    )
    service = CreationService(model="local-model", enable_vector_recall=False)

    result = await service.analyze_creation_skill(
        "共享空间流程说明",
        "# 背景与目标\n说明当前流程问题。\n## 方案设计\n明确角色、动作与验收证据。",
        "实施方案",
    )

    assert result["analysis_mode"] == "heuristic_fallback"
    assert result["fallback_reason"] == "invalid_model_output"


def test_creation_skill_title_abstracts_specific_departments_and_meeting_guide_suffix():
    result = CreationService._normalize_creation_skill_analysis(
        {
            "title": "商业化研发中心与电商产品部跨部门技术沟通会纪要撰写指南",
            "summary": "适用于多团队技术协作。",
        },
        "商业化研发中心与电商产品部跨部门技术沟通会纪要",
        "# 会议目标\n对齐系统架构和跨团队依赖。\n## 行动项\n明确后续安排。",
        "会议纪要",
    )

    assert result["title"] == "跨部门技术沟通会文档"
    assert "研发中心" not in result["title"]
    assert "产品部" not in result["title"]
    assert "研发中心" not in str(result)
    assert "产品部" not in str(result)


def test_creation_skill_title_ignores_incidental_review_words_in_document_body():
    result = CreationService._normalize_creation_skill_title(
        "技术架构设计文档",
        "通用运行平台整体技术方案",
        "# 背景\n正文引用了一段案例复盘和年度总结，但文档本身仍是技术方案。",
        "技术文档",
    )

    assert result == "运行平台整体技术方案"
    assert result != "项目复盘总结文档"


def test_creation_skill_normalizer_replaces_source_copies_and_private_examples():
    source_sentence = "星火项目组需要在三季度完成订单中台迁移并达到既定指标。"
    result = CreationService._normalize_creation_skill_analysis(
        {
            "title": "通用实施方案",
            "summary": source_sentence,
            "common_titles": ["星火项目组迁移方案"],
            "example_document": f"# 示例\n\n{source_sentence}\n\n## 计划\n\n沿用原有安排。",
        },
        "星火项目组三季度迁移方案",
        f"# 背景\n{source_sentence}\n## 计划\n分阶段执行并复核。",
        "实施方案",
    )

    serialized = str(result)
    assert "星火项目组" not in serialized
    assert source_sentence not in serialized
    assert result["field_examples"]["text_style"]
    assert len(result["example_document"]) >= 1000


def test_creation_skill_fallback_skips_main_title_and_compacts_private_placeholders():
    result = CreationService._fallback_creation_skill_analysis(
        "TieAgent OS 整体技术方案",
        """# TieAgent OS 整体技术方案

## 从业务视角看，TieAgent

### TieAgent OS 定义

### 核心目标

# TieAgent 评测接入文档

## runtime: TieAgent
""",
        "技术文档",
    )

    examples = result["field_examples"]["common_titles"]
    assert "目标对象 目标对象" not in str(examples)
    assert "协作工作台 OS 整体技术方案" in examples
    assert "从业务视角看，协作工作台的角色与边界" in examples
    assert "协作工作台 OS 定义" in examples
    assert "核心目标" in examples
    assert "runtime: 协作工作台的调度边界" in examples
    assert len(result["common_titles"]) >= 4
    assert len(result["text_style"]) >= 400
    assert result["title"] == "运行平台整体技术方案"
    assert result["distinctive_sections"][0]["title"] == "定义先行的概念建立"


def test_creation_skill_normalizer_replaces_short_model_sample_with_complete_fallback():
    result = CreationService._normalize_creation_skill_analysis(
        {
            "title": "流程优化文档",
            "summary": "适合需要梳理协作流程的场景。",
            "common_titles": ["标题简洁"],
            "text_style": "先写背景，再写方案。",
            "field_examples": {
                "common_titles": ["目标对象 目标对象 目标对象", "核心目标"],
            },
            "example_document": "# 示例\n\n## 背景\n\n内容很短。\n\n## 方案\n\n继续说明。",
        },
        "协作流程优化方案",
        "# 背景与目标\n说明问题。\n## 方案如何落到执行\n说明方案与验证方式。",
        "实施方案",
    )

    assert len(result["example_document"]) >= 1000
    assert result["example_document"].count("\n## ") >= 6
    assert "目标对象 目标对象" not in str(result["field_examples"]["common_titles"])
    assert len(result["common_titles"]) >= 4
    assert len(result["text_style"]) >= 260


def test_creation_skill_fallback_example_is_long_and_adapts_heading_style():
    result = CreationService._fallback_creation_skill_analysis(
        "通用流程说明",
        """# 通用流程说明

## 为什么需要调整

正文先给出问题判断。

## 方案如何落到执行

正文继续说明边界和动作。
""",
        "实施方案",
    )

    example = result["example_document"]
    assert len(example) >= 1000
    assert example.count("\n## ") >= 6
    assert "## 为什么现有预约方式需要调整" in example
    assert "## 方案如何落到执行" in example
    assert not any(ch.isascii() and ch.isdigit() for ch in example)


def test_creation_skill_style_content_stops_before_unrelated_bake_appendices():
    selected = CreationService._select_creation_skill_style_content(
        "TieAgent OS 整体技术方案",
        """# TieAgent OS 整体技术方案

## 从业务视角看，TieAgent

### TieAgent OS 定义

# TieAgent 评测接入文档

## runtime: TieAgent

# 近期业务规划与资源架构补充

## 项目复盘总结

这部分是后续追加的无关内容。
""",
    )

    assert "TieAgent 评测接入文档" in selected
    assert "近期业务规划" not in selected
    assert "项目复盘总结" not in selected


def test_creation_skill_style_content_keeps_normal_multi_part_documents():
    selected = CreationService._select_creation_skill_style_content(
        "共享空间优化方案",
        """# 共享空间优化方案

## 背景

说明现状。

# 方案设计

说明方案。

# 实施与验证

说明落地与验收。
""",
    )

    assert "# 方案设计" in selected
    assert "# 实施与验证" in selected


def test_creation_skill_normalizer_humanizes_object_arrays_and_keeps_dynamic_sections():
    result = CreationService._normalize_creation_skill_analysis(
        {
            "title": "项目复盘总结文档",
            "common_titles": [
                {"level": "一级标题", "pattern": "# [核心主题] + OS/整体技术方案"},
            ],
            "structure_pattern": [
                {"role": "先定义核心对象，再说明核心目标"},
            ],
            "distinctive_sections": [
                {
                    "title": "定义先行",
                    "description": "先解释核心对象的角色与边界，再进入技术方案。",
                    "guidance": "对象首次出现时先给通俗解释，再补职责边界和核心目标。",
                    "examples": [
                        "协作工作台可以理解为连接任务、角色与结果证据的统一入口。"
                    ],
                }
            ],
        },
        "TieAgent OS 整体技术方案",
        """# TieAgent OS 整体技术方案

## 从业务视角看，TieAgent

### TieAgent OS 定义

### 核心目标
""",
        "技术文档",
    )

    serialized = str(result)
    assert result["title"] == "运行平台整体技术方案"
    assert result["common_titles"][0].startswith("一级标题：采用“")
    assert "structure_pattern" not in result
    assert "structure_pattern" not in result["section_headings"]
    assert "structure_pattern" not in result["field_examples"]
    assert "{'level':" not in serialized
    assert "{'role':" not in serialized
    assert result["distinctive_sections"][0]["title"] == "定义先行"
