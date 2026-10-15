"""Time provenance checks for generated data prose."""

from creation.source_time_consistency import (
    audit_confirmed_report_period,
    audit_source_time_consistency,
    source_period_coverage,
)


CONFIRMED_WEEK = {
    "start": "2026-09-21", "end": "2026-09-27",
    "timezone": "Asia/Shanghai", "source": "user",
}


def test_confirmed_week_flags_wrong_target_and_in_period_misclassification():
    document = """检索未发现覆盖报告期（2026-09-28 至 2026-10-04）的会议纪要。
本周（2026-09-28）Token 使用情况仍需核对。
GPU 统计日期 2026-09-26（非目标报告周期），故只作参考。"""

    audit = audit_confirmed_report_period(document, CONFIRMED_WEEK)

    assert audit["status"] == "revise"
    assert [(issue["code"], issue["line_number"])
            for issue in audit["issues"]] == [
        ("report_target_period_conflict", 1),
        ("report_target_period_conflict", 2),
        ("in_period_date_marked_outside", 3),
    ]
    assert audit["issues"][0]["claimed_period"] == {
        "start": "2026-09-28", "end": "2026-10-04",
    }


def test_confirmed_week_keeps_source_and_collection_dates_separate():
    document = """本次报告周期为 2026-09-21 至 2026-09-27。
来源统计期为 2026-09-28 至 2026-10-04，属于后续数据。
来源页面于 2026-09-28 采集，统计日期为 2026-09-26。"""

    audit = audit_confirmed_report_period(document, CONFIRMED_WEEK)

    assert audit["status"] == "pass"
    assert audit["issues"] == []


def test_confirmed_week_checks_report_date_ranges_in_h1_title():
    document = """# 2026-09-28 至 2026-10-04 GPU 成本优化周报
本次报告周期为 2026-09-21 至 2026-09-27。"""

    audit = audit_confirmed_report_period(document, CONFIRMED_WEEK)

    assert audit["status"] == "revise"
    assert [(issue["code"], issue["line_number"], issue["claimed_period"])
            for issue in audit["issues"]] == [
        ("report_target_period_conflict", 1,
         {"start": "2026-09-28", "end": "2026-10-04"}),
    ]
    assert audit_confirmed_report_period(
        "# GPU 成本优化周报（2026年9月28日至10月4日）", CONFIRMED_WEEK
    )["status"] == "revise"
    assert audit_confirmed_report_period(
        "# 2026-09-28 GPU 成本优化周报", CONFIRMED_WEEK
    )["status"] == "revise"


def test_confirmed_week_h1_does_not_promote_source_or_capture_range():
    document = """# GPU 周报：来源统计期 2026-09-28 至 2026-10-04
# GPU 周报：页面采集日期 2026-09-28 至 2026-10-04
# 本周周报：来源统计期 2026-09-28 至 2026-10-04
# GPU 周报（2026-09-28 采集）
# GPU 周报（发布于 2026-09-28）
# GPU 周报（2026-09-21 至 2026-09-27）"""

    audit = audit_confirmed_report_period(document, CONFIRMED_WEEK)

    assert audit["status"] == "pass"
    assert audit["issues"] == []


def test_confirmed_week_requires_structured_user_confirmation():
    document = "报告期 2026-09-28 至 2026-10-04。"

    assert audit_confirmed_report_period(document, None)["status"] == "not_applicable"
    assert audit_confirmed_report_period(document, {
        **CONFIRMED_WEEK, "source": "agent_assumption",
    })["status"] == "not_applicable"
    for source in ("user", "user_edit", "confirmed_selection"):
        assert audit_confirmed_report_period(document, {
            **CONFIRMED_WEEK, "source": source,
        })["status"] == "revise"


def test_confirmed_week_rejects_wrong_week_number_but_keeps_source_week():
    audit = audit_confirmed_report_period(
        "本次报告周期为第40周。\n来源统计周期为第40周，仅作历史参考。",
        CONFIRMED_WEEK)

    assert audit["status"] == "revise"
    assert [(item["code"], item["line_number"])
            for item in audit["issues"]] == [("report_week_number_conflict", 1)]
    assert "第 39 周" in audit["issues"][0]["suggestion"]


def _result(source_id, title, url, claims, *, captured="2026-09-28"):
    return {
        "source_id": source_id,
        "source_kind": "report_url",
        "can_use": True,
        "title": title,
        "source_url": url,
        "collected_at": captured,
        "creation_evidence": {
            "validation_status": "verified",
            "validation": {"verified_claims": claims},
        },
        "structured_data": {"verified_claims": list(claims)},
    }


def _gpu_status():
    return _result(
        1582,
        "电商GPU信息平台 - GPU使用情况一览",
        "https://example.test/gpu/info",
        [{
            "label": "电商 GPU配额使用率",
            "value": "81.0%",
            "statistical_period": "2026-09-26",
        }],
    )


def _project_cost():
    return _result(
        1584,
        "电商GPU信息平台 - GPU项目用量管理",
        "https://example.test/gpu/project",
        [{
            "label": "项目甲 年化成本（万元）",
            "value": "806.4万元",
            "statistical_period": "",
        }],
    )


def test_metric_as_of_capture_day_conflicts_with_verified_page_day():
    document = "截至2026年9月28日，整体GPU配额使用率为**81.0%**。"

    audit = audit_source_time_consistency(document, [_gpu_status()])

    assert audit["status"] == "review"
    assert [(item["code"], item["source_id"], item["source_statistical_periods"])
            for item in audit["issues"]] == [
        ("statistical_period_conflict", 1582, ["2026-09-26"])
    ]
    assert audit["issues"][0]["claimed_statistical_date"] == "2026-09-28"


def test_collection_day_and_statistical_day_are_distinct_and_correct():
    document = (
        "该页于2026-09-28采集，统计日期为2026-09-26；"
        "整体GPU配额使用率为81.0%。"
    )

    audit = audit_source_time_consistency(document, [_gpu_status()])

    assert audit["status"] == "pass"
    assert audit["issues"] == []


def test_missing_page_period_does_not_become_capture_day_in_table_intro():
    document = """### 项目成本
下表列出项目年化成本，所有数值均来自电商GPU信息平台在2026-09-28周期的原始记录：
| 项目名称 | 年化成本（万元） |
| --- | ---: |
| 项目甲 | 806.4 |
来源：《电商GPU信息平台 - GPU项目用量管理》 (https://example.test/gpu/project)。
"""

    audit = audit_source_time_consistency(document, [_gpu_status(), _project_cost()])

    assert audit["status"] == "review"
    assert [(item["code"], item["source_id"], item["line_number"])
            for item in audit["issues"]] == [
        ("capture_date_promoted_to_statistical_period", 1584, 2)
    ]


def test_missing_page_period_can_be_described_honestly_as_unstated():
    document = (
        "来源：《电商GPU信息平台 - GPU项目用量管理》；"
        "页面于2026-09-28采集，未注明统计周期。"
    )

    audit = audit_source_time_consistency(document, [_project_cost()])

    assert audit["issues"] == []


def test_same_day_snapshot_inferred_from_capture_date_is_flagged():
    document = (
        "来源：《电商GPU信息平台 - GPU项目用量管理》"
        "（URL: https://example.test/gpu/project），"
        "采集时间为2026-09-28。统计周期为当日实时快照。"
    )

    audit = audit_source_time_consistency(document, [_project_cost()])

    assert [item["code"] for item in audit["issues"]] == [
        "capture_date_implied_as_statistical_period"
    ]


def test_metric_specific_period_is_used_for_a_qualified_source():
    source = _result(
        214,
        "运营看板",
        "https://example.test/token",
        [
            {"label": "独立部署输入Tokens", "value": "39,981亿",
             "statistical_period": "2026-09-27至2026-09-27"},
            {"label": "公共部署输出Tokens", "value": "68.19亿",
             "statistical_period": "2026-09-26至2026-09-26"},
        ],
    )
    correct = "2026-09-28采集、2026-09-27统计，独立部署输入Tokens为39,981亿。"
    wrong = "截至2026-09-28，独立部署输入Tokens为39,981亿。"

    assert audit_source_time_consistency(correct, [source])["issues"] == []
    issues = audit_source_time_consistency(wrong, [source])["issues"]
    assert [(item["code"], item["source_statistical_periods"]) for item in issues] == [
        ("statistical_period_conflict", ["2026-09-27"])
    ]


def test_period_mismatch_risk_supplies_actual_period_and_flags_unqualified_target():
    source = _result(
        214, "运营看板", "https://example.test/token",
        [{"label": "独立部署输入Tokens", "value": "39,981亿",
          "statistical_period": ""}],
    )
    source["data_risks"] = [{
        "kind": "period_mismatch",
        "label": "独立部署输入Tokens",
        "value": "39,981亿",
        "actual_period": "2026-09-27至2026-09-27",
        "expected_period": {"start": "2026-09-28", "end": "2026-09-28"},
    }]

    undated = audit_source_time_consistency(
        "本周独立部署输入Tokens为39,981亿。", [source]
    )
    falsely_dated = audit_source_time_consistency(
        "截至2026-09-28，独立部署输入Tokens为39,981亿。", [source]
    )
    qualified = audit_source_time_consistency(
        "本周独立部署输入Tokens使用9月27日统计参考值39,981亿；"
        "目标周期尚未覆盖。", [source]
    )

    assert [item["code"] for item in undated["issues"]] == [
        "qualified_period_presented_as_target"
    ]
    assert [(item["code"], item["source_statistical_periods"])
            for item in falsely_dated["issues"]] == [
        ("statistical_period_conflict", ["2026-09-27"])
    ]
    assert qualified["issues"] == []


def test_requested_period_is_not_misread_as_source_statistical_period():
    document = (
        "目标周期为2026-09-28，来源实际统计周期为2026-09-26；"
        "GPU配额使用率81.0%仅供参考。"
    )

    audit = audit_source_time_consistency(document, [_gpu_status()])

    assert audit["issues"] == []


def test_current_value_table_header_conflicts_even_when_rows_disclose_old_period():
    source = _result(
        214, "运营看板", "https://example.test/token",
        [{"label": "独立部署输入Tokens", "value": "39,981亿",
          "statistical_period": "2026-09-27至2026-09-27"}],
    )
    source["data_risks"] = [{
        "kind": "period_mismatch", "label": "独立部署输入Tokens",
        "value": "39,981亿", "actual_period": "2026-09-27至2026-09-27",
        "expected_period": {"start": "2026-09-28", "end": "2026-09-28"},
    }]
    document = """| 指标类型 | 本周显示值 (亿) | 数据来源与时间口径 |
| :--- | :---: | :--- |
| 独立部署输入Tokens | 39,981 | 2026-09-27（非目标周期） |
"""

    audit = audit_source_time_consistency(document, [source])

    assert [(item["code"], item["line_number"], item["metric_labels"])
            for item in audit["issues"]] == [
        ("qualified_period_in_current_value_column", 1, ["独立部署输入Tokens"])
    ]
    assert audit["issues"][0]["column_index"] == 1
    assert audit["issues"][0]["column_indices"] == [1]


def test_current_value_header_binds_only_the_old_source_column():
    old = _result(
        214, "旧来源", "https://example.test/old",
        [{"label": "共享指标", "value": "12亿",
          "statistical_period": "2026-09-27"}],
    )
    old["data_risks"] = [{
        "kind": "period_mismatch", "label": "共享指标", "value": "12亿",
        "actual_period": "2026-09-27",
    }]
    current = _result(
        215, "新来源", "https://example.test/current",
        [{"label": "共享指标", "value": "10亿",
          "statistical_period": "2026-09-28"}],
    )
    document = """| 指标 | 本周旧来源值（亿） | 本周新来源值（亿） |
| :--- | :---: | :---: |
| 共享指标 | 12 | 10 |
"""

    issues = audit_source_time_consistency(document, [old, current])["issues"]

    assert len(issues) == 1
    assert issues[0]["source_id"] == 214
    assert issues[0]["column_index"] == 1
    assert issues[0]["column_indices"] == [1]


def test_current_value_header_leaves_ambiguous_column_unbound():
    source = _result(
        214, "旧来源", "https://example.test/old",
        [{"label": "共享指标", "value": "12亿",
          "statistical_period": "2026-09-27"}],
    )
    source["data_risks"] = [{
        "kind": "period_mismatch", "label": "共享指标", "value": "12亿",
        "actual_period": "2026-09-27",
    }]
    duplicated_columns = """| 指标 | 本周甲值（亿） | 本周乙值（亿） |
| :--- | :---: | :---: |
| 共享指标 | 12 | 12 |
"""
    competing = _result(
        215, "另一来源", "https://example.test/current",
        [{"label": "共享指标", "value": "12亿",
          "statistical_period": "2026-09-28"}],
    )
    one_column = """| 指标 | 本周值（亿） |
| :--- | :---: |
| 共享指标 | 12 |
"""

    repeated = audit_source_time_consistency(duplicated_columns, [source])["issues"]
    competing_sources = audit_source_time_consistency(one_column, [source, competing])["issues"]

    assert len(repeated) == len(competing_sources) == 1
    assert repeated[0]["column_index"] is None
    assert repeated[0]["column_indices"] == []
    assert competing_sources[0]["column_index"] is None
    assert competing_sources[0]["column_indices"] == []


def test_historical_reference_table_header_is_not_a_current_period_claim():
    source = _result(
        214, "运营看板", "https://example.test/token",
        [{"label": "独立部署输入Tokens", "value": "39,981亿",
          "statistical_period": "2026-09-27至2026-09-27"}],
    )
    source["data_risks"] = [{
        "kind": "period_mismatch", "label": "独立部署输入Tokens",
        "value": "39,981亿", "actual_period": "2026-09-27至2026-09-27",
    }]
    document = """| 指标类型 | 历史参考值 (亿) | 真实统计日 |
| :--- | :---: | :--- |
| 独立部署输入Tokens | 39,981 | 2026-09-27 |
"""

    assert audit_source_time_consistency(document, [source])["issues"] == []


def test_ambiguous_same_numeric_value_is_not_attributed_to_a_source():
    other = _result(
        99, "另一平台", "https://example.test/other",
        [{"label": "GPU配额使用率", "value": "81.0%",
          "statistical_period": "2026-09-28"}],
    )

    audit = audit_source_time_consistency(
        "截至2026-09-28，整体GPU配额使用率为81.0%。",
        [_gpu_status(), other],
    )

    assert audit["issues"] == []


def test_unverified_report_is_not_used_as_a_temporal_authority():
    source = _gpu_status()
    source["creation_evidence"]["validation_status"] = "failed"

    audit = audit_source_time_consistency(
        "截至2026-09-28，GPU配额使用率为81.0%。", [source]
    )

    assert audit["status"] == "diagnostic"
    assert audit["issues"] == []


def test_invalid_timezone_is_diagnostic_not_a_false_date_conflict():
    audit = audit_source_time_consistency(
        "截至2026-09-28，GPU配额使用率为81.0%。",
        [_gpu_status()], timezone_name="Invalid/Zone",
    )

    assert audit["status"] == "diagnostic"
    assert audit["issues"] == []


def test_source_period_coverage_classifies_verified_claims_and_missing_periods():
    late = _result(214, "运营看板", "https://example.test/token", [
        {"label": "输入", "requested_metric": "输入Token", "value": "100亿",
         "statistical_period": "2026-09-27至2026-09-27"},
        {"label": "输出", "requested_metric": "输出Token", "value": "10亿",
         "statistical_period": "2026-09-28至2026-09-28"},
    ])
    missing = _project_cost()

    coverage = source_period_coverage(
        [late, missing], [214, 1584, 999],
        {"period_start": "2026-09-28", "period_end": "2026-09-28"},
    )

    assert coverage["status"] == "ok"
    assert coverage["claim_counts"] == {"matched": 1, "outside": 1, "unknown": 1}
    assert coverage["source_counts"] == {
        "matched": 0, "outside": 0, "unknown": 2, "mixed": 1,
    }
    assert [(item["source_id"], item["status"]) for item in coverage["sources"]] == [
        (214, "mixed"), (1584, "unknown"), ("999", "unknown")
    ]
    assert [claim["requested_metric"] for claim in coverage["sources"][0]["claims"]] == [
        "输入Token", "输出Token"
    ]


def test_source_period_coverage_uses_actual_risk_period_not_requested_period():
    source = _result(
        214, "运营看板", "https://example.test/token",
        [{"label": "输入", "value": "100亿", "statistical_period": ""}],
    )
    source["data_risks"] = [{
        "kind": "period_mismatch", "label": "输入", "value": "100亿",
        "actual_period": "2026-09-27至2026-09-27",
        "expected_period": {"start": "2026-09-28", "end": "2026-09-28"},
    }]

    coverage = source_period_coverage(
        [source], [214], {"period_start": "2026-09-28", "period_end": "2026-09-28"},
    )

    assert coverage["claim_counts"] == {"matched": 0, "outside": 1, "unknown": 0}
    assert coverage["sources"][0]["claims"][0]["statistical_period"] == "2026-09-27至2026-09-27"


def test_source_period_coverage_rejects_broader_aggregate_as_full_coverage():
    source = _result(
        1, "月报", "https://example.test/month",
        [{"label": "支出", "value": "100万元",
          "statistical_period": "2026-09-01至2026-09-30"}],
    )

    coverage = source_period_coverage(
        [source], [1], {"period_start": "2026-09-28", "period_end": "2026-09-28"},
    )

    assert coverage["sources"][0]["claims"][0]["reason"] == "statistical_period_partially_overlaps_target"
    assert coverage["claim_counts"] == {"matched": 0, "outside": 0, "unknown": 1}
