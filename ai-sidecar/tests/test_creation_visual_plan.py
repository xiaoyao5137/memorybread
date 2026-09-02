import json

from creation.visual_plan import (
    VISUAL_PLAN_SCHEMA_VERSION,
    normalize_visual_plan,
    parse_chapter_design_result,
)


def _diagram(diagram_id, section_title, diagram_type="flowchart_lr"):
    return {
        "id": diagram_id,
        "section_title": section_title,
        "purpose": "解释已有对象之间的关系",
        "diagram_type": diagram_type,
        "required": True,
        "reason": "连续文字难以同时表达对象、方向和边界",
        "source_points": ["对象甲调用对象乙", "对象乙返回处理结果"],
        "placement": "after_intro",
        "max_nodes": 12,
    }


def test_visual_plan_normalization_is_content_scoped_and_bounded():
    plan = normalize_visual_plan(
        {
            "policy": "auto",
            "max_diagrams": 99,
            "diagrams": [
                _diagram("relationship-overview", "关系总览"),
                _diagram("duplicate-section", "关系总览", "sequence"),
                {
                    **_diagram("unsupported", "另一个章节"),
                    "diagram_type": "vendor_specific_chart",
                },
                {
                    **_diagram("no-evidence", "证据不足章节"),
                    "source_points": [],
                },
            ],
        }
    )

    assert plan["schema_version"] == VISUAL_PLAN_SCHEMA_VERSION
    assert plan["max_diagrams"] == 8
    assert [item["id"] for item in plan["diagrams"]] == [
        "relationship-overview"
    ]


def test_chapter_design_parser_keeps_blueprint_and_visual_plan_separate():
    payload = {
        "blueprint_markdown": "1. 关系总览：解释对象和依赖。",
        "visual_plan": {
            "schema_version": VISUAL_PLAN_SCHEMA_VERSION,
            "policy": "auto",
            "max_diagrams": 3,
            "diagrams": [_diagram("relationship-overview", "关系总览")],
        },
    }

    blueprint, plan = parse_chapter_design_result(json.dumps(payload, ensure_ascii=False))

    assert blueprint.startswith("1. 关系总览")
    assert plan["diagrams"][0]["section_title"] == "关系总览"


def test_legacy_chapter_design_text_degrades_without_inventing_diagrams():
    blueprint, plan = parse_chapter_design_result("一、背景\n二、核心说明")

    assert blueprint == "一、背景\n二、核心说明"
    assert plan["diagrams"] == []
