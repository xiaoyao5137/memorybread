from rag.material_input import build_material_analysis_input


TABLE = """相对 Orion：耗时降低为+%。
服务层
名称 | Delay avg/P50/P99(ms) | 成功率
Orion | 10 / 15 / 20 | 99.2%
Aurora | 8（+20.0%） / 12（+20.0%） / 25（-25.0%） | 97.6%（-1.6 pp）
备注：峰值出现于部署期间，仍需复测。"""
CHART = """首段文本延迟 | ms | 完整响应时长 | ms
请求发出至结束，最近60秒已完成请求
— P50 | ---- P95 | 平均 | P99 | MAX
34.6s
15.7s | 26s
14m24s | 28m47s | 43m11s
查看读数 | 查看读数"""


def test_table_projection_replaces_raw_cells_and_removes_only_change_annotations():
    result = build_material_analysis_input("【图片7】\n" + TABLE + "\n【图片7结束】")
    assert "材料字段逐项对照" in result
    assert "图片7 / 服务层 / Aurora" in result
    assert "Delay avg（ms） = 8；相对基线：从10ms降至8ms" in result
    assert "Delay P99（ms） = 25；相对基线：从20ms升至25ms" in result
    assert "成功率 = 97.6%；相对基线：从99.2%降至97.6%" in result
    assert "+20.0%" not in result and "-25.0%" not in result and "-1.6 pp" not in result
    assert "名称 |" not in result and "Aurora |" not in result
    assert "备注：峰值出现于部署期间，仍需复测。" in result


def test_chart_projection_keeps_labels_and_time_scope_without_fabricating_readings():
    result = build_material_analysis_input(CHART)
    assert "首段文本延迟" in result and "完整响应时长" in result
    assert "最近60秒" in result
    assert "请求发出至结束，最近60秒已完成请求" in result
    for value in ["34.6", "15.7", "26s", "14m24s", "P50", "P95", "P99"]:
        assert value not in result
    assert "未确认各系列的具体数值、走势或状态" in result
    assert "未确认与其他图片的对应关系" in result


def test_each_image_uses_its_own_table_or_chart_projection():
    evidence = "【图片4】\n" + TABLE + "\n【图片4结束】\n【图片9】\n" + CHART + "\n【图片9结束】"
    result = build_material_analysis_input(evidence)
    first, second = result.split("【图片9】", 1)
    assert "材料字段逐项对照" in first
    assert "图表可确认文字与口径" in second
    assert "Delay avg" not in second
    assert "34.6" not in result


def test_ordinary_ocr_is_preserved_byte_for_byte():
    evidence = "【图片3】\n  服务返回 500 状态码。\n请检查配置，P99只是日志里的字段名。\n【图片3结束】"
    assert build_material_analysis_input(evidence) == evidence
    assert build_material_analysis_input("普通材料\n数字 123\n") == "普通材料\n数字 123\n"
    statistics = "P50 = 10ms\nP95 = 20ms\n100\n200"
    assert build_material_analysis_input(statistics) == statistics


def test_ambiguous_table_without_chart_signals_is_preserved():
    evidence = "名称 | Delay avg/P50/P99(ms) | Duration avg/P50/P99(ms)\nOrion | 1/2/3"
    assert build_material_analysis_input(evidence) == evidence


def test_other_non_table_narrative_and_literal_bar_are_retained():
    result = build_material_analysis_input(TABLE + "\n说明：A | B 是两个可选方案。\n操作说明与上述数据同等重要。")
    assert "说明：A | B 是两个可选方案。" in result
    assert "操作说明与上述数据同等重要。" in result
