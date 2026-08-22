from __future__ import annotations

import pytest

from creation.query_engine import (
    QUERY_PLAN_VERSION,
    QueryPlanError,
    execute_query_plan,
    parse_query_plan,
    relation_catalog,
    validate_query_plan,
)


def table_result(tables, *, complete=True):
    return {
        "source_id": 41,
        "title": "运行时发现的数据表",
        "source_url": "https://example.test/report",
        "can_use": True,
        "collected_at": 1000,
        "structured_data": {
            "tables": tables,
            "pagination": {
                "dataset_complete": complete,
                "pages_captured": len(tables),
                "total_rows": sum(max(0, len(table) - 1) for table in tables),
            },
        },
    }


def test_ranked_query_merges_pages_sorts_numerically_and_preserves_rows():
    header = ["对象", "分类", "金额", "说明"]
    data = table_result(
        [
            [header, ["甲", "A", "9.5万元", "甲说明"], ["乙", "B", "1,200万元", "乙说明"]],
            [header, ["丙", "A", "310万元", "丙说明"], ["丁", "B", "—", "丁说明"]],
        ]
    )
    catalog = relation_catalog([data])
    relation = catalog[0]
    field_ids = {item["display_name"]: item["field_id"] for item in relation["fields"]}
    plan = validate_query_plan(
        {
            "schema_version": QUERY_PLAN_VERSION,
            "mode": "relational",
            "relation_id": relation["relation_id"],
            "operations": [
                {"op": "order_by", "field_id": field_ids["金额"], "direction": "desc", "nulls": "last"},
                {"op": "project", "field_ids": [field_ids["对象"], field_ids["金额"], field_ids["说明"]]},
                {"op": "limit", "value": 3},
            ],
        },
        catalog,
    )

    result = execute_query_plan(plan, [data])

    object_field = field_ids["对象"]
    amount_field = field_ids["金额"]
    note_field = field_ids["说明"]
    assert [row["cells"][object_field]["raw"] for row in result["rows"]] == ["乙", "丙", "甲"]
    assert [row["cells"][amount_field]["normalized"] for row in result["rows"]] == ["1200", "310", "9.5"]
    assert result["rows"][0]["cells"][note_field]["raw"] == "乙说明"
    assert all(len(row["source_row_ids"]) == 1 for row in result["rows"])
    assert result["validation"]["status"] == "verified"


def test_group_aggregate_and_order_use_only_generic_operators():
    header = ["分组", "数值"]
    data = table_result(
        [[[header, ["A", "10"], ["B", "7"], ["A", "5"], ["B", "20"]][0],
          ["A", "10"], ["B", "7"], ["A", "5"], ["B", "20"]]]
    )
    catalog = relation_catalog([data])
    relation = catalog[0]
    fields = {item["display_name"]: item["field_id"] for item in relation["fields"]}
    plan = validate_query_plan(
        {
            "mode": "relational",
            "relation_id": relation["relation_id"],
            "operations": [
                {"op": "group_by", "field_ids": [fields["分组"]]},
                {"op": "aggregate", "field_id": fields["数值"], "function": "sum", "alias": "合计"},
                {"op": "order_by", "field_id": "合计", "direction": "desc"},
            ],
        },
        catalog,
    )

    result = execute_query_plan(plan, [data])

    assert [row["cells"][fields["分组"]]["raw"] for row in result["rows"]] == ["B", "A"]
    assert [row["cells"]["合计"]["normalized"] for row in result["rows"]] == ["27", "15"]
    assert [len(row["source_row_ids"]) for row in result["rows"]] == [2, 2]


def test_relation_merge_preserves_legitimate_duplicate_rows():
    header = ["分组", "数值"]
    data = table_result(
        [[header, ["A", "10"]], [header, ["A", "10"]]],
        complete=True,
    )
    catalog = relation_catalog([data])
    relation = catalog[0]
    fields = {item["display_name"]: item["field_id"] for item in relation["fields"]}
    plan = validate_query_plan(
        {
            "mode": "relational",
            "relation_id": relation["relation_id"],
            "operations": [
                {"op": "group_by", "field_ids": [fields["分组"]]},
                {
                    "op": "aggregate",
                    "field_id": fields["数值"],
                    "function": "sum",
                    "alias": "合计",
                },
                {
                    "op": "project",
                    "field_ids": [fields["分组"], "合计"],
                },
            ],
        },
        catalog,
    )

    result = execute_query_plan(plan, [data])

    assert result["rows"][0]["cells"]["合计"]["normalized"] == "20"
    assert len(result["rows"][0]["source_row_ids"]) == 2


def test_incomplete_relation_fails_closed_for_global_ordering():
    data = table_result([[['名称', '值'], ['甲', '2'], ['乙', '1']]], complete=False)
    catalog = relation_catalog([data])
    relation = catalog[0]
    value_field = relation["fields"][1]["field_id"]
    plan = validate_query_plan(
        {
            "mode": "relational",
            "relation_id": relation["relation_id"],
            "operations": [
                {"op": "order_by", "field_id": value_field, "direction": "desc"},
                {"op": "limit", "value": 2},
            ],
        },
        catalog,
    )

    result = execute_query_plan(plan, [data])

    assert result["validation"]["status"] == "insufficient_coverage"
    assert "ORDER_REQUIRES_COMPLETE_DATASET" in result["validation"]["errors"]


def test_complete_relation_with_fewer_rows_than_limit_is_still_valid():
    data = table_result([[['名称', '值'], ['甲', '2'], ['乙', '1']]], complete=True)
    catalog = relation_catalog([data])
    relation = catalog[0]
    plan = validate_query_plan(
        {
            "mode": "relational",
            "relation_id": relation["relation_id"],
            "operations": [{"op": "limit", "value": 10}],
        },
        catalog,
    )

    result = execute_query_plan(plan, [data])

    assert len(result["rows"]) == 2
    assert result["validation"]["status"] == "verified"


def test_incomplete_relation_fails_closed_for_aggregate():
    data = table_result([[['名称', '值'], ['甲', '2'], ['乙', '1']]], complete=False)
    catalog = relation_catalog([data])
    relation = catalog[0]
    plan = validate_query_plan(
        {
            "mode": "relational",
            "relation_id": relation["relation_id"],
            "operations": [
                {
                    "op": "aggregate",
                    "field_id": relation["fields"][1]["field_id"],
                    "function": "sum",
                    "alias": "总计",
                }
            ],
        },
        catalog,
    )

    result = execute_query_plan(plan, [data])

    assert result["validation"]["status"] == "insufficient_coverage"
    assert "OPERATION_REQUIRES_COMPLETE_DATASET" in result["validation"]["errors"]


def test_narrative_plan_keeps_non_table_analysis_path_available():
    plan = validate_query_plan(
        {
            "mode": "narrative",
            "presentation": "prose",
            "reason": "需要解释而非关系运算",
            "operations": [],
        },
        [],
    )
    result = execute_query_plan(plan, [])
    assert result["shape"] == "narrative"
    assert result["plan"]["presentation"] == "prose"
    assert result["validation"]["status"] == "not_applicable"


def test_relational_execution_preserves_chart_presentation_intent():
    data = table_result([[['时间', '数值'], ['一', '2'], ['二', '5']]])
    catalog = relation_catalog([data])
    relation = catalog[0]
    plan = validate_query_plan(
        {
            "mode": "relational",
            "presentation": "chart",
            "relation_id": relation["relation_id"],
            "operations": [],
        },
        catalog,
    )

    result = execute_query_plan(plan, [data])

    assert result["plan"]["output"]["presentation"] == "chart"
    assert result["validation"]["status"] == "verified"


def test_plan_rejects_model_invented_field():
    data = table_result([[['名称', '值'], ['甲', '2']]])
    catalog = relation_catalog([data])
    with pytest.raises(QueryPlanError) as error:
        validate_query_plan(
            {
                "mode": "relational",
                "relation_id": catalog[0]["relation_id"],
                "operations": [
                    {"op": "order_by", "field_id": "invented.field", "direction": "desc"}
                ],
            },
            catalog,
        )
    assert error.value.code == "PLAN_FIELD_NOT_FOUND"


def test_parse_plan_accepts_fenced_json_and_relation_discovery_skips_text_only_data():
    parsed = parse_query_plan(
        """```json
        {"schema_version":"memorybread.data-query-plan.v1","mode":"narrative","operations":[]}
        ```"""
    )
    assert parsed["mode"] == "narrative"
    assert relation_catalog([{"source_id": 9, "can_use": True, "structured_data": {"summary": "only text"}}]) == []
