from rag.material_summary import build_material_comparison_summary


def test_complete_metric_names_units_and_direction_are_never_interchanged():
    bindings = """- 图片1 / 模型层 / Orion：
  - 调用 E2E avg（s） = 6.37
  - 调用 E2E P95（s） = 20.45
  - 调用 E2E P99（s） = 32.76
- 图片1 / 模型层 / Aurora：
  - 调用 E2E avg（s） = 5.09（+20.1%）；相对基线：从6.37s降至5.09s
  - 调用 E2E P95（s） = 16.29（+20.3%）；相对基线：从20.45s降至16.29s
  - 调用 E2E P99（s） = 32.08（+2.1%）；相对基线：从32.76s降至32.08s"""
    result = build_material_comparison_summary(bindings, "")
    assert "基线读数→对应对象读数" in result
    assert result.endswith("以上比较仅覆盖材料中能够明确确认的字段。")
    assert "调用 E2E P99（s）下降：32.76s→32.08s" in result
    assert "调用 E2E avg（s）下降：6.37s→5.09s" in result
    assert "TTFT" not in result and "上升" not in result
    assert "+20.1%" not in result
    assert "Aurora：这些可比较的耗时指标均低于表中其他对象" in result


def test_tail_increases_and_decreases_are_both_reported_exactly():
    bindings = """- 图片5 / 服务层 / Aurora：
  - TTFT avg（s） = 12.92；相对基线：从15.21s降至12.92s
  - TTFT P99（s） = 60.28；相对基线：从56.56s升至60.28s
  - E2E P95（s） = 67.84；相对基线：从59.91s升至67.84s
  - E2E P99（s） = 121.03；相对基线：从131.41s降至121.03s"""
    result = build_material_comparison_summary(bindings, "")
    assert "TTFT P99（s）上升：56.56s→60.28s" in result
    assert "E2E P95（s）上升：59.91s→67.84s" in result
    assert "E2E P99（s）下降：131.41s→121.03s" in result
    assert "其他对象" not in result  # One row cannot establish the lowest object.


def test_percentage_changes_are_copied_without_relative_percentage_calculation():
    bindings = """- 图片2 / 层A / Aurora：
  - Cache = 64.94%；相对基线：从64.62%升至64.94%
  - 成功率 = 97.6%（-1.6 pp）；相对基线：从99.2%降至97.6%"""
    result = build_material_comparison_summary(bindings, "")
    assert "Cache上升：64.62%→64.94%" in result
    assert "成功率下降：99.2%→97.6%" in result
    assert "百分点" not in result


def test_lowest_claim_requires_identical_complete_field_sets_and_units():
    bindings = """- 图A / 层A / Orion：
  - Delay avg（ms） = 10
  - Delay P99（ms） = 30
- 图A / 层A / Aurora：
  - Delay avg（ms） = 5；相对基线：从10ms降至5ms"""
    assert "其他对象" not in build_material_comparison_summary(bindings, "")
    mismatched = bindings + "\n  - Delay P99（s） = 0.02；相对基线：从0.03s降至0.02s"
    assert "其他对象" not in build_material_comparison_summary(mismatched, "")


def test_ties_or_a_single_worse_field_prevent_a_lowest_claim():
    template = """- 图A / 层A / Orion：
  - Delay avg（ms） = 10
  - Delay P99（ms） = 30
- 图A / 层A / Aurora：
  - Delay avg（ms） = 5；相对基线：从10ms降至5ms
  - Delay P99（ms） = VALUE"""
    for value in ["30", "40；相对基线：从30ms升至40ms"]:
        assert "其他对象" not in build_material_comparison_summary(template.replace("VALUE", value), "")


def test_chart_statement_only_uses_safe_analysis_branch_and_preserves_unknowns():
    bindings = "- 图片1 / 层A / Aurora：\n  - Delay avg（ms） = 5；相对基线：从10ms降至5ms"
    analysis = """【图片2】
图表可确认文字与口径：
首段文本延迟
完整响应时长
最近60秒已完成请求
本图尚未确认各系列的具体数值、走势或状态，也未确认与其他图片的对应关系。
【图片2结束】"""
    result = build_material_comparison_summary(bindings, analysis)
    assert "首段文本延迟" in result and "最近60秒已完成请求" in result
    assert "不足以确认曲线数值、走势、各系列状态" in result
    assert "与其他图片的对应关系" in result
    assert "稳定" not in result


def test_absent_or_inconsistent_comparisons_return_empty_for_normal_answering():
    assert build_material_comparison_summary("- 图A / Orion：\n  - Delay avg（ms） = 10", "") == ""
    malformed = "- 图A / Aurora：\n  - Delay avg（ms） = 5；相对基线：从10ms升至5ms"
    assert build_material_comparison_summary(malformed, "") == ""


def test_no_cross_layer_lowest_comparison_or_throughput_inference():
    bindings = """- 图A / 层A / Orion：
  - Delay avg（ms） = 10
- 图A / 层B / Aurora：
  - Delay avg（ms） = 5；相对基线：从20ms降至5ms
  - QPS = 2；相对基线：从1升至2"""
    result = build_material_comparison_summary(bindings, "")
    assert "其他对象" not in result
    assert "QPS" not in result and "吞吐" not in result and "瓶颈" not in result
