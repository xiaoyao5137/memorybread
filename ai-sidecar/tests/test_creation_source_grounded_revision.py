from creation.source_grounded_revision import revise_source_grounded_preview


def test_revision_retracts_only_audited_lines_and_verified_rank_sentence():
    document = (
        "# 周报\n\n## 数据\n\n"
        "截至2026-09-28，资源使用率为81.0%。\n"
        "| 指标 | 本周显示值 |\n| :--- | :--- |\n| 输入 | 12亿 |\n"
        "采集时间为2026-09-28。统计周期为当日实时快照。\n"
        "已核实的其它事实。项目甲虽未进入Top 2，但值得关注。\n"
    )
    issues = [
        {"code": "statistical_period_conflict", "source_id": 41,
         "line_number": 5, "excerpt": "截至2026-09-28，资源使用率为81.0%。",
         "claimed_statistical_date": "2026-09-28",
         "metric_labels": ["资源使用率"], "source_statistical_periods": ["2026-09-26"],
         "capture_date": "2026-09-28"},
        {"code": "qualified_period_in_current_value_column", "source_id": 42,
         "line_number": 6, "excerpt": "| 指标 | 本周显示值 |",
         "source_statistical_periods": ["2026-09-27"],
         "column_index": 1, "column_indices": [1]},
        {"code": "capture_date_implied_as_statistical_period", "source_id": 43,
         "line_number": 9,
         "excerpt": "采集时间为2026-09-28。统计周期为当日实时快照。",
         "capture_date": "2026-09-28", "source_statistical_periods": []},
        {"code": "statistical_period_unverified", "source_id": 44,
         "line_number": 8, "excerpt": "| 输入 | 12亿 |",
         "source_statistical_periods": []},
        {"code": "unrecognized_audit_code", "source_id": 45,
         "line_number": 10, "excerpt": "已核实的其它事实。",
         "source_statistical_periods": []},
    ]
    ranks = [{"row_id": "row-1", "relation_id": "table-1", "name": "项目甲",
              "limit": 2, "excerpt": "项目甲虽未进入Top 2，但值得关注"}]
    revised, edits = revise_source_grounded_preview(document, issues, ranks)
    assert len(edits) == 4
    assert "来源统计期为 2026-09-26" in revised
    assert "来源页显示值（统计期 2026-09-27）" in revised
    assert "采集于 2026-09-28，但未注明指标统计期" in revised
    assert "已核实的其它事实。已验证的前 2 行包含「项目甲」" in revised
    assert "未进入Top 2" not in revised
    assert "统计周期为当日实时快照" not in revised
    assert "| :--- | :--- |" in revised


def test_revision_never_changes_protected_base_or_unmatched_excerpt():
    base = "# 已确认文档\n\n## 已完成章节\n\n截至2026-09-28，已确认原文。"
    draft = base + "\n\n## 当前章节\n\n截至2026-09-28，待核验指标。"
    issues = [
        {"code": "statistical_period_conflict", "source_id": 41,
         "line_number": 5, "excerpt": "截至2026-09-28，已确认原文。",
         "source_statistical_periods": ["2026-09-26"]},
        {"code": "statistical_period_conflict", "source_id": 42,
         "line_number": 9, "excerpt": "正文已被用户编辑",
         "source_statistical_periods": ["2026-09-26"]},
    ]
    revised, edits = revise_source_grounded_preview(
        draft, issues, [], protected_base=base)
    assert revised == draft
    assert edits == []
    changed, notes = revise_source_grounded_preview(
        "# 用户新版本", issues, [], protected_base=base)
    assert changed == "# 用户新版本"
    assert notes == [{"status": "skipped", "reason": "protected_base_changed"}]


def test_qualified_header_revises_only_audited_column():
    document = "| 指标 | 本周已核实值 | 本周显示值 |\n| :--- | :--- | :--- |\n| 输入 | 12亿 | 11亿 |"
    issue = {"code": "qualified_period_in_current_value_column",
             "line_number": 1, "excerpt": "| 指标 | 本周已核实值 | 本周显示值 |",
             "source_statistical_periods": ["2026-09-27"],
             "column_index": 2, "column_indices": [2]}
    revised, edits = revise_source_grounded_preview(document, [issue], [])
    assert len(edits) == 1
    assert "| 本周已核实值 | 来源页显示值（统计期 2026-09-27） |" in revised
    uncertain, uncertain_edits = revise_source_grounded_preview(
        document, [{**issue, "column_index": None, "column_indices": []}], [])
    assert uncertain == document
    assert uncertain_edits == []

    both = [
        {**issue, "column_index": 1, "column_indices": [1],
         "source_statistical_periods": ["2026-09-26"]},
        {**issue, "column_index": 2, "column_indices": [2],
         "source_statistical_periods": ["2026-09-27"]},
    ]
    twice, two_edits = revise_source_grounded_preview(document, both, [])
    assert len(two_edits) == 2
    assert "| 来源页显示值（统计期 2026-09-26） | 来源页显示值（统计期 2026-09-27） |" in twice


def test_rank_revision_keeps_valid_facts_before_and_after_exact_clause():
    document = ("2024年项目乙未进入Top 2。\n"
                "已核实的项目甲仍在榜中。项目乙未进入Top 2，但是成本仍高。")
    conflict = {"name": "项目乙", "limit": 2, "row_id": "r2",
                "relation_id": "current-table", "line_number": 2,
                "excerpt": "项目乙未进入Top 2"}
    revised, edits = revise_source_grounded_preview(document, [], [conflict])
    assert len(edits) == 1
    assert revised.splitlines()[0] == "2024年项目乙未进入Top 2。"
    assert "已核实的项目甲仍在榜中" in revised
    assert "但是成本仍高" in revised
    assert "已验证的前 2 行包含「项目乙」" in revised


def test_date_revision_preserves_other_facts_in_same_paragraph():
    document = (
        "截至2026年9月28日，指标A为81%。指标B的来源已核实为50%。\n"
        "页面采集时间为2026-09-28。统计周期为当日实时快照。第三项说明仍有效。"
    )
    issues = [
        {"code": "statistical_period_conflict", "source_id": 1,
         "line_number": 1, "excerpt": document.splitlines()[0],
         "claimed_statistical_date": "2026-09-28",
         "source_statistical_periods": ["2026-09-26"]},
        {"code": "capture_date_implied_as_statistical_period", "source_id": 2,
         "line_number": 2, "excerpt": document.splitlines()[1],
         "source_statistical_periods": [], "capture_date": "2026-09-28"},
    ]
    revised, edits = revise_source_grounded_preview(document, issues, [])
    assert len(edits) == 2
    assert "指标B的来源已核实为50%" in revised
    assert "页面采集时间为2026-09-28" in revised
    assert "第三项说明仍有效" in revised
    assert "截至2026年9月28日，指标A为81%" not in revised
    assert "统计周期为当日实时快照" not in revised


def test_ambiguous_multi_fact_date_line_is_left_for_delivery_review():
    document = "截至2026-09-28，A为81%；截至2026-09-28，B为50%。"
    issue = {"code": "statistical_period_conflict", "line_number": 1,
             "excerpt": document, "claimed_statistical_date": "2026-09-28",
             "source_statistical_periods": ["2026-09-26"]}
    revised, edits = revise_source_grounded_preview(document, [issue], [])
    assert revised == document
    assert edits == []
