"""通用数据查询计划与确定性关系执行器。

模型只负责把自然语言绑定到运行时发现的 relation/field 标识；本模块负责
校验白名单算子、执行关系变换并保留行级来源。模块不包含任何业务字段名。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


QUERY_PLAN_VERSION = "memorybread.data-query-plan.v1"
QUERY_RESULT_VERSION = "memorybread.data-query-result.v1"
MAX_RELATIONS = 12
MAX_COLUMNS = 64
MAX_SAMPLE_ROWS = 3
MAX_OPERATIONS = 16
MAX_RESULT_ROWS = 1000

ALLOWED_MODES = {"relational", "narrative"}
ALLOWED_PRESENTATIONS = {"auto", "table", "chart", "prose", "metric"}
ALLOWED_OPERATORS = {
    "filter",
    "project",
    "order_by",
    "distinct",
    "group_by",
    "aggregate",
    "limit",
}
ALLOWED_FILTER_OPERATORS = {
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "in",
    "is_null",
    "not_null",
}
ALLOWED_AGGREGATIONS = {"count", "count_distinct", "sum", "avg", "min", "max"}


class QueryPlanError(ValueError):
    """可向 Planner 反馈的稳定计划错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _compact_text(value: Any, maximum: int = 160) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text if len(text) <= maximum else text[:maximum].rstrip() + "..."


def _deduplicate_headers(values: list[Any]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, raw in enumerate(values[:MAX_COLUMNS]):
        base = _compact_text(raw, 120) or "column_{}".format(index + 1)
        count = seen.get(base, 0) + 1
        seen[base] = count
        headers.append(base if count == 1 else "{} ({})".format(base, count))
    return headers


def _decimal_value(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    text = _compact_text(value, 240)
    if not text or text in {"-", "—", "--", "null", "None"}:
        return None
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", text)
    if match is None:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def _normalized_cell(value: Any) -> Any:
    if _compact_text(value, 240) in {"", "-", "—", "--", "null", "None"}:
        return None
    decimal = _decimal_value(value)
    if decimal is not None:
        return format(decimal, "f")
    text = _compact_text(value, 600)
    return text if text else None


def _infer_type(values: list[Any]) -> str:
    present = [value for value in values if _compact_text(value)]
    if not present:
        return "unknown"
    if all(_decimal_value(value) is not None for value in present):
        return "decimal"
    lowered = {_compact_text(value).lower() for value in present}
    if lowered <= {"true", "false", "yes", "no", "是", "否"}:
        return "boolean"
    return "string"


def discover_relations(data_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从任意可用数据结果发现关系表；不依据标题或业务字段做判断。"""
    relations: list[dict[str, Any]] = []
    for result in data_results:
        if not isinstance(result, dict) or result.get("can_use") is not True:
            continue
        structured = result.get("structured_data")
        if not isinstance(structured, dict):
            continue
        tables = structured.get("tables")
        if not isinstance(tables, list):
            continue
        source_id = result.get("source_id")
        # 分页采集会产生多个表头相同的页面片段。按完整 Schema 合并关系，
        # 而不是按页暴露给 Planner。相同值的业务行仍须保留，不能按内容去重；
        # 页面去重由采集器使用分页状态标识负责。
        grouped_tables: list[tuple[int, list[Any]]] = []
        grouped_positions: dict[tuple[str, ...], int] = {}
        for table_index, table in enumerate(tables):
            if not isinstance(table, list) or len(table) < 2:
                continue
            header = table[0]
            if not isinstance(header, list) or not header:
                continue
            header_key = tuple(_compact_text(item, 120) for item in header[:MAX_COLUMNS])
            position = grouped_positions.get(header_key)
            if position is None:
                grouped_positions[header_key] = len(grouped_tables)
                grouped_tables.append((table_index, [header, *table[1:]]))
                continue
            _, grouped = grouped_tables[position]
            for row in table[1:]:
                if isinstance(row, list):
                    grouped.append(row)

        for table_index, table in grouped_tables:
            header = table[0]
            headers = _deduplicate_headers(header)
            raw_rows = [row for row in table[1:] if isinstance(row, list)]
            if not raw_rows:
                continue
            relation_id = "source_{}.table_{}".format(source_id, table_index)
            fields = []
            for column_index, display_name in enumerate(headers):
                samples = [
                    row[column_index]
                    for row in raw_rows[:24]
                    if column_index < len(row) and _compact_text(row[column_index])
                ][:MAX_SAMPLE_ROWS]
                fields.append(
                    {
                        "field_id": "{}.col_{}".format(relation_id, column_index),
                        "display_name": display_name,
                        "type": _infer_type(samples),
                        "samples": [_compact_text(item) for item in samples],
                    }
                )
            pagination = structured.get("pagination")
            relation = {
                "relation_id": relation_id,
                "source_id": source_id,
                "source_title": _compact_text(result.get("title"), 200),
                "source_url": result.get("source_url"),
                "collected_at": result.get("collected_at"),
                "observed_at": result.get("observed_at"),
                "table_index": table_index,
                "captured_row_count": len(raw_rows),
                "fields": fields,
                "_rows": raw_rows,
            }
            if isinstance(pagination, dict):
                relation["pagination"] = dict(pagination)
            relations.append(relation)
            if len(relations) >= MAX_RELATIONS:
                return relations
    return relations


def relation_catalog(data_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回可安全进入 Planner Prompt 的轻量 Schema 目录。"""
    return [
        {key: value for key, value in relation.items() if not key.startswith("_")}
        for relation in discover_relations(data_results)
    ]


def parse_query_plan(value: str) -> dict[str, Any]:
    text = value.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise QueryPlanError("PLAN_INVALID_JSON", "Planner 未返回合法 JSON") from exc
    if not isinstance(payload, dict):
        raise QueryPlanError("PLAN_INVALID_SHAPE", "QueryPlan 必须是对象")
    return payload


def validate_query_plan(
    plan: dict[str, Any], catalog: list[dict[str, Any]]
) -> dict[str, Any]:
    mode = str(plan.get("mode") or "narrative").strip().lower()
    if mode not in ALLOWED_MODES:
        raise QueryPlanError("PLAN_MODE_UNSUPPORTED", "QueryPlan mode 不受支持")
    normalized: dict[str, Any] = {
        "schema_version": QUERY_PLAN_VERSION,
        "mode": mode,
        "reason": _compact_text(plan.get("reason"), 400),
    }
    presentation = str(plan.get("presentation") or "auto").strip().lower()
    if presentation not in ALLOWED_PRESENTATIONS:
        raise QueryPlanError(
            "PLAN_PRESENTATION_UNSUPPORTED",
            "QueryPlan presentation 不受支持",
        )
    normalized["presentation"] = presentation
    if mode == "narrative":
        normalized["operations"] = []
        return normalized

    relations = {str(item.get("relation_id")): item for item in catalog}
    relation_id = str(plan.get("relation_id") or "").strip()
    relation = relations.get(relation_id)
    if relation is None:
        raise QueryPlanError("PLAN_RELATION_NOT_FOUND", "QueryPlan 引用了不存在的 relation")
    fields = {
        str(item.get("field_id")): item
        for item in relation.get("fields", [])
        if isinstance(item, dict)
    }
    known_fields = dict(fields)
    normalized["relation_id"] = relation_id
    normalized["source_id"] = relation.get("source_id")

    operations = plan.get("operations") or []
    if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
        raise QueryPlanError("PLAN_OPERATIONS_INVALID", "QueryPlan operations 数量无效")
    normalized_operations = []
    for raw in operations:
        if not isinstance(raw, dict):
            raise QueryPlanError("PLAN_OPERATOR_INVALID", "QueryPlan operator 必须是对象")
        operator = str(raw.get("op") or "").strip().lower()
        if operator not in ALLOWED_OPERATORS:
            raise QueryPlanError("PLAN_OPERATOR_UNSUPPORTED", "QueryPlan 包含未授权算子")
        item: dict[str, Any] = {"op": operator}
        if operator in {"filter", "order_by"}:
            field_id = str(raw.get("field_id") or "").strip()
            if field_id not in known_fields:
                raise QueryPlanError("PLAN_FIELD_NOT_FOUND", "QueryPlan 引用了不存在的字段")
            item["field_id"] = field_id
        if operator == "filter":
            comparison = str(raw.get("operator") or "eq").strip().lower()
            if comparison not in ALLOWED_FILTER_OPERATORS:
                raise QueryPlanError("PLAN_FILTER_UNSUPPORTED", "filter 比较符不受支持")
            item["operator"] = comparison
            if comparison not in {"is_null", "not_null"}:
                item["value"] = raw.get("value")
        elif operator == "order_by":
            direction = str(raw.get("direction") or "asc").strip().lower()
            nulls = str(raw.get("nulls") or "last").strip().lower()
            if direction not in {"asc", "desc"} or nulls not in {"first", "last"}:
                raise QueryPlanError("PLAN_ORDER_INVALID", "order_by 参数无效")
            item.update({"direction": direction, "nulls": nulls})
        elif operator in {"project", "distinct", "group_by"}:
            field_ids = raw.get("field_ids") or []
            if not isinstance(field_ids, list) or not field_ids:
                raise QueryPlanError("PLAN_FIELDS_REQUIRED", "该算子必须声明 field_ids")
            resolved = [str(field_id) for field_id in field_ids]
            if any(field_id not in known_fields for field_id in resolved):
                raise QueryPlanError("PLAN_FIELD_NOT_FOUND", "QueryPlan 引用了不存在的字段")
            item["field_ids"] = list(dict.fromkeys(resolved))
        elif operator == "aggregate":
            function = str(raw.get("function") or "").strip().lower()
            if function not in ALLOWED_AGGREGATIONS:
                raise QueryPlanError("PLAN_AGGREGATE_UNSUPPORTED", "aggregate 函数不受支持")
            field_id = str(raw.get("field_id") or "").strip()
            if function != "count" and field_id not in known_fields:
                raise QueryPlanError("PLAN_FIELD_NOT_FOUND", "aggregate 引用了不存在的字段")
            alias = _compact_text(raw.get("alias"), 120) or "{}_{}".format(
                function, field_id.rsplit(".", 1)[-1] if field_id else "rows"
            )
            item.update(
                {
                    "function": function,
                    "field_id": field_id or None,
                    "alias": alias,
                }
            )
            known_fields[alias] = {
                "field_id": alias,
                "display_name": alias,
                "type": "decimal",
                "derived": True,
            }
        elif operator == "limit":
            try:
                limit = int(raw.get("value"))
            except (TypeError, ValueError) as exc:
                raise QueryPlanError("PLAN_LIMIT_INVALID", "limit 必须是整数") from exc
            if not 1 <= limit <= MAX_RESULT_ROWS:
                raise QueryPlanError("PLAN_LIMIT_INVALID", "limit 超出允许范围")
            item["value"] = limit
        normalized_operations.append(item)
    normalized["operations"] = normalized_operations
    normalized["output"] = {
        "shape": "table",
        "presentation": presentation,
        "preserve_source_rows": not any(
            item["op"] in {"group_by", "aggregate"} for item in normalized_operations
        ),
    }
    return normalized


def _field_index(field_id: str) -> int:
    try:
        return int(field_id.rsplit(".col_", 1)[1])
    except (IndexError, ValueError) as exc:
        raise QueryPlanError("PLAN_FIELD_INVALID", "field_id 格式无效") from exc


def _compare_value(value: Any) -> tuple[int, Any]:
    if value is None or _compact_text(value, 240) in {"", "-", "—", "--", "null", "None"}:
        return (2, "")
    decimal = _decimal_value(value)
    if decimal is not None:
        return (0, decimal)
    return (1, _compact_text(value).casefold())


def _filter_matches(value: Any, operator: str, expected: Any) -> bool:
    missing = value is None or not _compact_text(value)
    if operator == "is_null":
        return missing
    if operator == "not_null":
        return not missing
    if operator == "in":
        candidates = expected if isinstance(expected, list) else [expected]
        return any(_filter_matches(value, "eq", candidate) for candidate in candidates)
    if operator == "contains":
        return _compact_text(expected).casefold() in _compact_text(value).casefold()
    left_decimal = _decimal_value(value)
    right_decimal = _decimal_value(expected)
    left: Any = left_decimal if left_decimal is not None and right_decimal is not None else _compact_text(value).casefold()
    right: Any = right_decimal if left_decimal is not None and right_decimal is not None else _compact_text(expected).casefold()
    return {
        "eq": left == right,
        "ne": left != right,
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
    }[operator]


def _aggregate_values(function: str, values: list[Any]) -> Any:
    if function == "count":
        return len([value for value in values if _compact_text(value)])
    if function == "count_distinct":
        return len({_compact_text(value) for value in values if _compact_text(value)})
    decimals = [value for value in (_decimal_value(item) for item in values) if value is not None]
    if not decimals:
        return None
    if function == "sum":
        return format(sum(decimals, Decimal("0")), "f")
    if function == "avg":
        return format(sum(decimals, Decimal("0")) / Decimal(len(decimals)), "f")
    if function == "min":
        return format(min(decimals), "f")
    if function == "max":
        return format(max(decimals), "f")
    return None


def execute_query_plan(
    plan: dict[str, Any], data_results: list[dict[str, Any]]
) -> dict[str, Any]:
    relations = discover_relations(data_results)
    catalog = [
        {key: value for key, value in relation.items() if not key.startswith("_")}
        for relation in relations
    ]
    normalized = validate_query_plan(plan, catalog)
    if normalized["mode"] == "narrative":
        return {
            "schema_version": QUERY_RESULT_VERSION,
            "shape": "narrative",
            "plan": normalized,
            "rows": [],
            "validation": {"status": "not_applicable", "errors": []},
        }

    relation = next(
        item for item in relations if item["relation_id"] == normalized["relation_id"]
    )
    fields = relation["fields"]
    field_map = {item["field_id"]: item for item in fields}
    rows = []
    for row_index, raw in enumerate(relation["_rows"]):
        cells = {
            field["field_id"]: (raw[index] if index < len(raw) else None)
            for index, field in enumerate(fields)
        }
        rows.append(
            {
                "row_id": "{}:row_{}".format(relation["relation_id"], row_index),
                "cells": cells,
                "source_row_ids": ["{}:row_{}".format(relation["relation_id"], row_index)],
            }
        )

    projected_ids = [field["field_id"] for field in fields]
    requested_projection: Optional[list[str]] = None
    group_ids: list[str] = []
    aggregates: list[dict[str, Any]] = []
    order_operations: list[dict[str, Any]] = []
    requested_limit: Optional[int] = None
    for operation in normalized["operations"]:
        operator = operation["op"]
        if operator == "filter":
            rows = [
                row
                for row in rows
                if _filter_matches(
                    row["cells"].get(operation["field_id"]),
                    operation["operator"],
                    operation.get("value"),
                )
            ]
        elif operator == "project":
            requested_projection = operation["field_ids"]
            projected_ids = operation["field_ids"]
        elif operator == "distinct":
            seen = set()
            distinct_rows = []
            for row in rows:
                key = tuple(_compact_text(row["cells"].get(field_id)) for field_id in operation["field_ids"])
                if key in seen:
                    continue
                seen.add(key)
                distinct_rows.append(row)
            rows = distinct_rows
        elif operator == "group_by":
            group_ids = operation["field_ids"]
        elif operator == "aggregate":
            aggregates.append(operation)
        elif operator == "order_by":
            order_operations.append(operation)
        elif operator == "limit":
            requested_limit = operation["value"]

    result_schema = []
    if aggregates:
        grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        effective_groups = group_ids or []
        for row in rows:
            key = tuple(_compact_text(row["cells"].get(field_id)) for field_id in effective_groups)
            grouped.setdefault(key, []).append(row)
        if not grouped and not effective_groups:
            grouped[()] = []
        aggregated_rows = []
        for group_key, member_rows in grouped.items():
            cells = {
                field_id: group_key[index]
                for index, field_id in enumerate(effective_groups)
            }
            for aggregate in aggregates:
                values = (
                    [row["cells"].get(aggregate["field_id"]) for row in member_rows]
                    if aggregate.get("field_id")
                    else [1 for _ in member_rows]
                )
                cells[aggregate["alias"]] = _aggregate_values(aggregate["function"], values)
            aggregated_rows.append(
                {
                    "row_id": "{}:group_{}".format(relation["relation_id"], len(aggregated_rows)),
                    "cells": cells,
                    "source_row_ids": [source_id for row in member_rows for source_id in row["source_row_ids"]],
                }
            )
        rows = aggregated_rows
        result_schema = [dict(field_map[field_id]) for field_id in effective_groups]
        result_schema.extend(
            {
                "field_id": aggregate["alias"],
                "display_name": aggregate["alias"],
                "type": "decimal",
                "derived": True,
                "function": aggregate["function"],
            }
            for aggregate in aggregates
        )
        derived_field_map = {item["field_id"]: item for item in result_schema}
        projected_ids = requested_projection or [item["field_id"] for item in result_schema]
        try:
            result_schema = [derived_field_map[field_id] for field_id in projected_ids]
        except KeyError as exc:
            raise QueryPlanError(
                "PLAN_PROJECTION_INVALID",
                "聚合结果只能投影分组字段或派生指标",
            ) from exc
    else:
        result_schema = [dict(field_map[field_id]) for field_id in projected_ids]

    for operation in reversed(order_operations):
        field_id = operation["field_id"]
        reverse = operation["direction"] == "desc"
        nulls_first = operation["nulls"] == "first"
        present_rows = [
            row
            for row in rows
            if _compare_value(row["cells"].get(field_id))[0] != 2
        ]
        missing_rows = [
            row
            for row in rows
            if _compare_value(row["cells"].get(field_id))[0] == 2
        ]
        present_rows.sort(
            key=lambda row: _compare_value(row["cells"].get(field_id)),
            reverse=reverse,
        )
        rows = (
            [*missing_rows, *present_rows]
            if nulls_first
            else [*present_rows, *missing_rows]
        )
    available_before_limit = len(rows)
    if requested_limit is not None:
        rows = rows[:requested_limit]

    rendered_rows = [
        {
            "row_id": row["row_id"],
            "source_row_ids": row["source_row_ids"],
            "cells": {
                field_id: {
                    "raw": row["cells"].get(field_id),
                    "normalized": _normalized_cell(row["cells"].get(field_id)),
                }
                for field_id in projected_ids
            },
        }
        for row in rows
    ]
    pagination = relation.get("pagination") if isinstance(relation.get("pagination"), dict) else {}
    dataset_complete = pagination.get("dataset_complete") is True
    total_rows = pagination.get("total_rows")
    complete_dataset_operations = {
        "filter",
        "order_by",
        "distinct",
        "group_by",
        "aggregate",
    }
    global_operations = sorted(
        {
            item["op"]
            for item in normalized["operations"]
            if item["op"] in complete_dataset_operations
        }
    )
    coverage_status = "complete" if dataset_complete else "captured_relation_only"
    errors = []
    if "order_by" in global_operations and not dataset_complete:
        errors.append("ORDER_REQUIRES_COMPLETE_DATASET")
    elif global_operations and not dataset_complete:
        errors.append("OPERATION_REQUIRES_COMPLETE_DATASET")
    if (
        requested_limit is not None
        and available_before_limit < requested_limit
        and not dataset_complete
    ):
        errors.append("LIMIT_NOT_SATISFIED")
    return {
        "schema_version": QUERY_RESULT_VERSION,
        "shape": "table",
        "plan": normalized,
        "schema": result_schema,
        "rows": rendered_rows,
        "coverage": {
            "status": coverage_status,
            "captured_rows": relation["captured_row_count"],
            "total_rows": total_rows,
            "dataset_complete": dataset_complete,
        },
        "provenance": {
            "source_id": relation["source_id"],
            "title": relation["source_title"],
            "source_url": relation.get("source_url"),
            "collected_at": relation.get("collected_at"),
            "observed_at": relation.get("observed_at"),
            "relation_id": relation["relation_id"],
        },
        "validation": {
            "status": "verified" if not errors else "insufficient_coverage",
            "errors": errors,
            "global_operations": global_operations,
            "row_identity_preserved": normalized["output"]["preserve_source_rows"],
        },
    }


def build_query_planner_prompts(
    objective: str, catalog: list[dict[str, Any]]
) -> tuple[str, str]:
    """构造领域无关的语义编译 Prompt。"""
    system = """你是数据查询规划器。你的任务只是把自然语言目标绑定到本轮实际发现的关系表和字段，输出一个 JSON QueryPlan；不要回答问题、计算结果或生成 Markdown。

若目标需要精确筛选、排序、排名、去重、分组、聚合或限定行数，mode 使用 relational，并且只能引用目录中真实存在的 relation_id/field_id。
若目标只是概括、解释、归因、开放式分析，或目录不足以支持确定性关系运算，mode 使用 narrative。
presentation 只描述最终表达意图，可用 auto/table/chart/prose/metric；它不改变数据运算。用户未明确指定表达形式时使用 auto。
允许算子只有 filter、project、order_by、distinct、group_by、aggregate、limit。filter.operator 仅允许 eq/ne/gt/gte/lt/lte/contains/in/is_null/not_null；aggregate.function 仅允许 count/count_distinct/sum/avg/min/max。
不要根据固定行业词汇做判断；依据用户目标、字段显示名、类型和样例完成语义绑定。不得虚构字段。只输出 JSON 对象。"""
    user = json.dumps(
        {
            "schema_version": QUERY_PLAN_VERSION,
            "objective": objective,
            "relation_catalog": catalog,
            "output_contract": {
                "schema_version": QUERY_PLAN_VERSION,
                "mode": "relational | narrative",
                "presentation": "auto | table | chart | prose | metric",
                "relation_id": "required when relational",
                "operations": "ordered array of allowed operators",
                "reason": "short public explanation",
            },
        },
        ensure_ascii=False,
    )
    return system, user
