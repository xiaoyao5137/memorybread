"""Structured transcription must preserve metric names, layers and ambiguity."""
from rag.material_evidence import build_material_value_bindings


def test_missing_preceding_column_does_not_shift_composite_metrics():
    text = """【图片1】
智能体层
任务名称 | GPU | 副本数 | TTFT avg / P50 / P95 / P99（s） | E2E avg / P50 / P95 / P99（s） | 成功率
FP8 EAGLE | 4 | 11.53（+24.2%） / 9.47（+28.3%） / 30.86（+15.0%） / 42.14（+25.5%） | 21.60（+13.7%） / 16.18（+22.9%） / 57.95（+3.3%） / 168.18（-28.0%） | 97.6%（-1.6 pp）
模型层
任务名称 | GPU | 调用 E2E avg / P50 / P95 / P99（s） | Cache
FP8 EAGLE | 1 | 5.09（+20.1%） / 2.69（+19.8%） / 16.29（+20.3%） / 32.08（+2.1%） | 64.94%
【图片1结束】"""
    result = build_material_value_bindings(text)
    assert "图片1 / 智能体层 / FP8 EAGLE" in result
    assert "TTFT avg（s） = 11.53（+24.2%）" in result
    assert "TTFT P99（s） = 42.14（+25.5%）" in result
    assert "E2E P99（s） = 168.18（-28.0%）" in result
    assert "图片1 / 模型层 / FP8 EAGLE" in result
    assert "调用 E2E avg（s） = 5.09（+20.1%）" in result
    assert "成功率 =" not in result  # Short row: no positional guessing.
    assert "Cache = 64.94%" in result


def test_merged_tail_recovers_only_the_first_complete_annotated_reading():
    result = build_material_value_bindings("""任务名称 | TTFT avg/P50/P95/P99(s) | E2E avg/P50/P95/P99(s) | 成功率
Orion | 1 / 2 / 3 / 4 | 11.84（+52.7%） / 8.91（+57.5%） / 32.28（+46.1%） / 55.17（+58.0%） 99.6%（+0.4 pp）""")
    assert "E2E avg（s） = 11.84（+52.7%）" in result
    assert "E2E P95（s） = 32.28（+46.1%）" in result
    assert "TTFT P99（s） = 4" in result
    assert "E2E P99（s） = 55.17（+58.0%）" in result
    assert "99.6" not in result


def test_one_cell_matching_two_identical_header_shapes_is_ambiguous():
    assert build_material_value_bindings("""Name | Latency avg/P50/P99 | Duration avg/P50/P99
Orion | 1 / 2 / 3""") == ""


def test_extra_same_shape_cell_is_not_assigned_by_guessing():
    assert build_material_value_bindings("""Name | Latency avg/P50/P99 | Duration avg/P50/P99
Orion | 1/2/3 | 4/5/6 | 7/8/9""") == ""


def test_unique_shape_can_be_recovered_when_a_different_metric_is_missing():
    result = build_material_value_bindings("""Name | Latency avg/P50/P95/P99 | Duration avg/min/max
Aurora | 7 / 8 / 9""")
    assert "Duration avg = 7" in result
    assert "Latency" not in result


def test_generic_chinese_composite_names_units_and_explicit_values_are_preserved():
    result = build_material_value_bindings("""【图片3】
存储层
项目 | 响应时间 平均/最小/最大（ms） | 成功率
星河平台 | 2.50（-3.0%） / 1.00 / 8.00 | 98.0%""")
    assert "图片3 / 存储层 / 星河平台" in result
    assert "响应时间 平均（ms） = 2.50（-3.0%）" in result
    assert "响应时间 最大（ms） = 8.00" in result
    assert "成功率 = 98.0%" in result


def test_curve_legends_and_axis_ticks_do_not_generate_statistics():
    text = """【图片2】
Agent 首段文本延迟 | mS | 完整响应时长 | mS
— P50 | ---- P95 | 平均 | ---- P75 | P99 | MAX
34.6s
15.7s | 26s
14m 24s | 28m 47s | 43m 11s | 57m 35s
查看读数 | 查看读数"""
    assert build_material_value_bindings(text) == ""


def test_plain_unlabelled_values_do_not_create_a_table():
    assert build_material_value_bindings("Orion | 1 / 2 / 3\nAurora | 4 / 5 / 6") == ""


def test_unsupported_table_header_ends_previous_metrics_and_baseline():
    result = build_material_value_bindings("""Relative to Orion:
Name | Latency avg/max(s)
Orion | 10 / 20
Component | Input/Output (bytes)
Aurora | 100 / 200
Name | Duration avg/max(ms)
Orion | 8 / 12
Aurora | 4 / 6""")
    assert "Latency avg（s） = 10" in result
    assert "100" not in result and "200" not in result
    assert "Duration avg（ms） = 4；相对基线：从8ms降至4ms" in result
    assert "从10s" not in result


def test_markdown_separator_does_not_end_a_recognized_table():
    result = build_material_value_bindings("""Name | Delay avg/max(ms)
:--- | ---:
Orion | 5 / 8""")
    assert "Delay avg（ms） = 5" in result


def test_unreadable_row_does_not_allow_later_values_to_reuse_uncertain_headers():
    result = build_material_value_bindings("""Name | Delay avg/max(ms)
Orion | 5 / 8
unknown | unreadable / unreadable
Aurora | 100 / 200""")
    assert "Delay avg（ms） = 5" in result
    assert "Aurora" not in result


def test_explicit_baseline_uses_values_instead_of_the_annotation_sign():
    result = build_material_value_bindings("""【图片1】
相对 BASE （BF16）：耗时降低为+%。
智能体层
名称 | E2E avg/P50/P95/P99(s)
BASE（BF16） | 25.04 / 20.99 / 59.91 / 131.41
tp1•DFlash2 | 21.84（+12.8%） / 15.45（+26.4%） / 67.84（-13.2%） / 121.03（+7.9%）
tp2•DFlash2 | 11.84（+52.7%） / 8.91（+57.5%） / 32.28（+46.1%） / 55.17（+58.0%） 99.6%（+0.4pp）""")
    assert "E2E P95（s） = 67.84（-13.2%）；相对基线：从59.91s升至67.84s" in result
    assert "E2E P99（s） = 121.03（+7.9%）；相对基线：从131.41s降至121.03s" in result
    assert "E2E P99（s） = 55.17（+58.0%）；相对基线：从131.41s降至55.17s" in result
    assert "增加13.2%" not in result and "减少58.0%" not in result
    assert "99.6" not in result


def test_first_row_is_not_assumed_to_be_the_baseline_and_explicit_baseline_can_follow():
    without = "Name | Delay avg/max(ms)\nAurora | 5 / 8\nOrion | 10 / 20"
    assert "相对基线" not in build_material_value_bindings(without)
    with_baseline = "Relative to Orion:\n" + without
    result = build_material_value_bindings(with_baseline)
    assert "Delay avg（ms） = 5；相对基线：从10ms降至5ms" in result


def test_baseline_scope_is_independent_for_each_image_layer_and_table():
    result = build_material_value_bindings("""【图片1】
相对 Orion:
智能体层
Name | Delay avg/max(ms)
Orion | 10 / 20
Aurora | 5 / 8
模型层
Name | Delay avg/max(ms)
Orion | 100 / 200
Aurora | 5 / 8
【图片1结束】
【图片2】
Name | Delay avg/max(ms)
Orion | 1000 / 2000
Aurora | 5 / 8
【图片2结束】""")
    assert "从10ms降至5ms" in result
    assert "从100ms降至5ms" in result
    assert "相对基线" not in result.split("- 图片2 / Orion：", 1)[1]


def test_success_percentage_preserves_annotation_and_only_adds_numeric_direction():
    result = build_material_value_bindings("""相对 Orion：
Name | Delay avg/max(s) | Success
Orion | 10 / 20 | 99.2%
Aurora | 5 / 8 | 97.6%（-1.6 pp）""")
    assert "Success = 97.6%（-1.6 pp）；相对基线：从99.2%降至97.6%" in result
    assert "个百分点" not in result


def test_nonexact_or_duplicate_baseline_name_does_not_produce_comparison():
    table = "Name | Delay avg/max(s)\nOrion v2 | 10 / 20\nAurora | 5 / 8"
    assert "相对基线" not in build_material_value_bindings("相对 Orion：\n" + table)
    duplicated = "相对 Orion v2：\n" + table + "\nOrion v2 | 30 / 40"
    assert "相对基线" not in build_material_value_bindings(duplicated)


def test_mixed_tail_with_prose_or_unbalanced_annotation_is_not_recovered():
    for tail in ["55.17（+58.0%） 不清楚99.6%", "55.17（+58.0% 99.6%", "55.17 99.6%"]:
        result = build_material_value_bindings("Name | Delay avg/max(s)\nOrion | 2 / " + tail)
        assert "Delay max" not in result


def test_unit_mismatch_and_zero_baseline_do_not_invent_percentage_comparison():
    result = build_material_value_bindings("""relative to Orion:
Name | Delay avg/max(ms)
Orion | 0 / 20ms
Aurora | 5 / 8s""")
    assert "Delay avg（ms） = 5；相对基线：从0ms升至5ms" in result
    assert "Delay max（ms） = 8s\n" in result + "\n"
