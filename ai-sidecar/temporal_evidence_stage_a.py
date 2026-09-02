"""阶段 A 的离线时间证据原型与评测工具。

该模块不接入 MemoryBread 运行链路，不读写用户数据库，只处理提交在仓库内的
合成测试语料。Python 语法以 3.9 为兼容基线。
"""

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA_VERSION = "memorybread.temporal-evidence-stage-a.v1"
CONTRACT_VERSION = "temporal-claim-v1"
RULE_VERSION = "temporal-gate-v1"

DECISIONS = {"eligible", "background_only", "unresolved", "conflict"}
SOURCE_TYPES = (
    "document",
    "meeting",
    "chat",
    "timeline",
    "work_memory",
    "data_snapshot",
)
STATUS_STRENGTH = {
    "unknown": 0,
    "planned": 1,
    "in_progress": 2,
    "blocked": 2,
    "cancelled": 2,
    "completed": 3,
}
EVENT_DATE_ROLES = {"event", "completion", "status_change", "statistical_period"}
NON_EVENT_DATE_ROLES = {
    "revision",
    "publication",
    "deadline",
    "goal_horizon",
    "historical_reference",
    "observation",
    "unknown",
}
ACTUAL_MODALITIES = {"actual", "observed"}

FULL_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*(?:[-/.]|年)\s*(\d{1,2})\s*"
    r"(?:[-/.]|月)\s*(\d{1,2})\s*日?(?!\d)"
)


def normalize_date_text(value: str) -> Optional[str]:
    """把正文中的一个完整公历日期规范为 ISO 日期。"""
    match = FULL_DATE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return date(
            int(match.group(1)), int(match.group(2)), int(match.group(3))
        ).isoformat()
    except ValueError:
        return None


def all_explicit_dates(text: str) -> List[str]:
    values = []
    for match in FULL_DATE_RE.finditer(str(text or "")):
        try:
            value = date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            ).isoformat()
        except ValueError:
            continue
        if value not in values:
            values.append(value)
    return values


def period_contains(period_start: str, period_end: str, value: Optional[str]) -> bool:
    return bool(value and period_start <= value <= period_end)


def _status_grounded_by_quote(quote: str) -> str:
    text = str(quote or "")
    if any(word in text for word in ("取消", "终止", "不再推进")):
        return "cancelled"
    if any(word in text for word in ("阻塞", "受阻", "等待依赖")):
        return "blocked"
    if any(word in text for word in ("已经完成", "已完成", "完成验收", "正式上线")):
        return "completed"
    if any(word in text for word in ("建设中", "进行中", "正在推进", "开始实施")):
        return "in_progress"
    if any(word in text for word in ("计划", "目标", "预计", "拟于", "截止")):
        return "planned"
    return "unknown"


def _quote_is_grounded(source_text: str, quote: str) -> bool:
    compact_source = re.sub(r"\s+", "", str(source_text or ""))
    compact_quote = re.sub(r"\s+", "", str(quote or ""))
    return bool(compact_quote and compact_quote in compact_source)


def baseline_decision(case: Dict[str, Any]) -> Dict[str, Any]:
    """模拟现有整篇日期匹配：整篇出现一个本期日期即放行。

    该基线刻意不读取 gold/candidate，仅体现当前 reference_period_evidence 的
    文档级能力边界；它不是产品源码的完整重放。
    """
    dates = all_explicit_dates(case["source_text"])
    start = case["task"]["period_start"]
    end = case["task"]["period_end"]
    if any(period_contains(start, end, value) for value in dates):
        decision = "eligible"
        reason = "whole_document_contains_in_period_date"
    elif dates:
        decision = "background_only"
        reason = "all_document_dates_outside_period"
    else:
        decision = "unresolved"
        reason = "document_has_no_complete_date"
    return {
        "id": case["id"],
        "decision": decision,
        "reason": reason,
        "explicit_dates": dates,
    }


def validate_candidate(
    case: Dict[str, Any], candidate: Dict[str, Any]
) -> Tuple[bool, List[str]]:
    """对模型/提取器提出的单个事项执行 fail-closed 回证。"""
    failures = []
    quote = str(candidate.get("evidence_quote") or "")
    if not _quote_is_grounded(case["source_text"], quote):
        failures.append("evidence_quote_not_grounded")

    status = str(candidate.get("status") or "unknown")
    grounded_status = _status_grounded_by_quote(quote)
    if status not in STATUS_STRENGTH:
        failures.append("invalid_status")
    elif grounded_status != "unknown" and (
        STATUS_STRENGTH.get(status, 0) > STATUS_STRENGTH.get(grounded_status, 0)
        or (status in {"blocked", "cancelled"} and status != grounded_status)
    ):
        failures.append("status_stronger_than_evidence")

    date_role = str(candidate.get("date_role") or "unknown")
    event_date = candidate.get("event_date")
    time_basis = str(candidate.get("time_basis") or "unknown")
    if date_role not in EVENT_DATE_ROLES | NON_EVENT_DATE_ROLES:
        failures.append("invalid_date_role")
    if event_date:
        try:
            date.fromisoformat(str(event_date))
        except ValueError:
            failures.append("invalid_event_date")
        if date_role not in EVENT_DATE_ROLES:
            failures.append("non_event_date_promoted_to_event")
        if time_basis == "explicit_text" and str(event_date) not in all_explicit_dates(
            case["source_text"]
        ):
            failures.append("event_date_not_grounded")
        if time_basis == "observation_time":
            failures.append("observation_promoted_to_event")
    elif date_role in EVENT_DATE_ROLES and time_basis not in {
        "unknown",
        "conflict",
    }:
        failures.append("event_role_without_date")

    modality = str(candidate.get("modality") or "unknown")
    if modality not in {
        "actual",
        "observed",
        "planned",
        "target",
        "forecast",
        "unknown",
    }:
        failures.append("invalid_modality")
    return not failures, failures


def prototype_decision(
    case: Dict[str, Any], candidate: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """事项级用途判断原型。候选默认取合成语料中的冻结提取候选。"""
    item = dict(candidate or case["candidate"])
    valid, failures = validate_candidate(case, item)
    if not valid:
        return {
            "id": case["id"],
            "decision": "unresolved",
            "reason": failures[0],
            "failures": failures,
            "candidate": item,
        }

    if bool(item.get("has_conflict")) or item.get("time_basis") == "conflict":
        return {
            "id": case["id"],
            "decision": "conflict",
            "reason": "unresolved_source_conflict",
            "candidate": item,
        }

    purpose = case["task"]["purpose"]
    event_date = item.get("event_date")
    event_in_period = period_contains(
        case["task"]["period_start"], case["task"]["period_end"], event_date
    )
    claim_kind = str(item.get("claim_kind") or "background")
    modality = str(item.get("modality") or "unknown")
    date_role = str(item.get("date_role") or "unknown")

    if purpose == "background":
        decision = "eligible"
        reason = "background_task_accepts_historical_claim"
    elif purpose == "metric":
        if (
            claim_kind == "metric"
            and modality in ACTUAL_MODALITIES
            and date_role == "statistical_period"
            and event_in_period
        ):
            decision = "eligible"
            reason = "actual_metric_matches_requested_period"
        elif modality in {"target", "forecast", "planned"} or claim_kind in {
            "goal",
            "forecast",
        }:
            decision = "background_only"
            reason = "non_actual_metric"
        elif not event_date:
            decision = "unresolved"
            reason = "metric_period_unknown"
        else:
            decision = "background_only"
            reason = "metric_outside_requested_period"
    else:
        if modality not in ACTUAL_MODALITIES:
            decision = "background_only"
            reason = "non_actual_claim_not_period_progress"
        elif claim_kind not in {"progress_event", "status_change", "decision"}:
            decision = "background_only"
            reason = "claim_kind_not_period_progress"
        elif date_role not in EVENT_DATE_ROLES or not event_date:
            decision = "unresolved"
            reason = "event_time_unknown"
        elif event_in_period:
            decision = "eligible"
            reason = "actual_event_matches_requested_period"
        else:
            decision = "background_only"
            reason = "event_outside_requested_period"

    return {
        "id": case["id"],
        "decision": decision,
        "reason": reason,
        "candidate": item,
    }


def load_corpus(path: Path) -> List[Dict[str, Any]]:
    cases = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("line %d has unsupported schema" % line_number)
            cases.append(item)
    return cases


def validate_corpus(cases: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    errors = []
    ids = set()
    source_counts = Counter()
    family_counts = Counter()
    split_counts = Counter()
    decision_counts = Counter()
    total = 0
    for case in cases:
        total += 1
        case_id = str(case.get("id") or "")
        if not case_id or case_id in ids:
            errors.append("duplicate_or_empty_id:%s" % case_id)
        ids.add(case_id)
        if case.get("source_type") not in SOURCE_TYPES:
            errors.append("%s:invalid_source_type" % case_id)
        expected = case.get("expected") or {}
        if expected.get("decision") not in DECISIONS:
            errors.append("%s:invalid_expected_decision" % case_id)
        if case.get("split") not in {"development", "holdout"}:
            errors.append("%s:invalid_split" % case_id)
        if not str(case.get("source_text") or "").strip():
            errors.append("%s:empty_source_text" % case_id)
        if (
            not _quote_is_grounded(
                str(case.get("source_text") or ""),
                str((case.get("candidate") or {}).get("evidence_quote") or ""),
            )
            and case.get("scenario_family") != "hallucinated_quote"
        ):
            errors.append("%s:candidate_quote_not_grounded" % case_id)
        source_counts[case.get("source_type")] += 1
        family_counts[case.get("scenario_family")] += 1
        split_counts[case.get("split")] += 1
        decision_counts[expected.get("decision")] += 1
    return {
        "valid": not errors,
        "errors": errors,
        "total": total,
        "source_counts": dict(sorted(source_counts.items())),
        "scenario_family_counts": dict(sorted(family_counts.items())),
        "split_counts": dict(sorted(split_counts.items())),
        "expected_decision_counts": dict(sorted(decision_counts.items())),
    }


def _safe_ratio(numerator: int, denominator: int) -> Optional[float]:
    if not denominator:
        return None
    return round(numerator / denominator, 6)


def evaluate_predictions(
    cases: List[Dict[str, Any]], predictions: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    total = len(cases)
    correct = 0
    false_admissions = 0
    predicted_eligible = 0
    expected_non_eligible = 0
    retained = 0
    expected_eligible = 0
    unresolved = 0
    failures = []
    per_source = {}
    per_family = {}

    for case in cases:
        expected = case["expected"]["decision"]
        actual = (predictions.get(case["id"]) or {}).get("decision", "missing")
        if expected == actual:
            correct += 1
        else:
            failures.append(
                {
                    "id": case["id"],
                    "source_type": case["source_type"],
                    "scenario_family": case["scenario_family"],
                    "expected": expected,
                    "actual": actual,
                }
            )
        if expected == "eligible":
            expected_eligible += 1
            if actual == "eligible":
                retained += 1
        else:
            expected_non_eligible += 1
            if actual == "eligible":
                false_admissions += 1
        if actual == "eligible":
            predicted_eligible += 1
        if actual in {"unresolved", "missing"}:
            unresolved += 1

        for key, bucket in (
            (case["source_type"], per_source),
            (case["scenario_family"], per_family),
        ):
            stats = bucket.setdefault(
                key,
                {"total": 0, "correct": 0, "expected_eligible": 0, "retained": 0},
            )
            stats["total"] += 1
            stats["correct"] += int(expected == actual)
            stats["expected_eligible"] += int(expected == "eligible")
            stats["retained"] += int(expected == "eligible" and actual == "eligible")

    for bucket in (per_source, per_family):
        for stats in bucket.values():
            stats["accuracy"] = _safe_ratio(stats["correct"], stats["total"])
            stats["retention"] = _safe_ratio(
                stats["retained"], stats["expected_eligible"]
            )

    return {
        "total": total,
        "correct": correct,
        "accuracy": _safe_ratio(correct, total),
        "false_admissions": false_admissions,
        "predicted_eligible": predicted_eligible,
        "false_admission_rate": _safe_ratio(false_admissions, predicted_eligible),
        "noneligible_leak_rate": _safe_ratio(false_admissions, expected_non_eligible),
        "expected_eligible": expected_eligible,
        "retained_eligible": retained,
        "retention_rate": _safe_ratio(retained, expected_eligible),
        "unresolved_or_missing": unresolved,
        "abstention_rate": _safe_ratio(unresolved, total),
        "per_source": dict(sorted(per_source.items())),
        "per_family": dict(sorted(per_family.items())),
        "failures": failures,
    }


def evaluate_extraction_semantics(
    cases: List[Dict[str, Any]], predictions: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    total = len(cases)
    candidate_count = 0
    grounded_quotes = 0
    validator_passed = 0
    event_date_exact = 0
    date_role_exact = 0
    modality_exact = 0
    status_not_enhanced = 0
    for case in cases:
        prediction = predictions.get(case["id"]) or {}
        item = prediction.get("candidate")
        if not isinstance(item, dict):
            continue
        candidate_count += 1
        quote = str(item.get("evidence_quote") or "")
        if _quote_is_grounded(case["source_text"], quote):
            grounded_quotes += 1
        valid, _failures = validate_candidate(case, item)
        if valid:
            validator_passed += 1
        expected = case["expected"]
        event_date_exact += int(item.get("event_date") == expected.get("event_date"))
        date_role_exact += int(item.get("date_role") == expected.get("date_role"))
        modality_exact += int(item.get("modality") == expected.get("modality"))
        grounded_status = _status_grounded_by_quote(quote)
        actual_status = str(item.get("status") or "unknown")
        status_not_enhanced += int(
            actual_status in STATUS_STRENGTH
            and (
                grounded_status == "unknown"
                or STATUS_STRENGTH[actual_status] <= STATUS_STRENGTH[grounded_status]
            )
        )
    return {
        "total": total,
        "candidate_count": candidate_count,
        "candidate_coverage": _safe_ratio(candidate_count, total),
        "grounded_quote_rate": _safe_ratio(grounded_quotes, candidate_count),
        "validator_pass_rate": _safe_ratio(validator_passed, candidate_count),
        "event_date_exact_rate": _safe_ratio(event_date_exact, candidate_count),
        "date_role_exact_rate": _safe_ratio(date_role_exact, candidate_count),
        "modality_exact_rate": _safe_ratio(modality_exact, candidate_count),
        "status_non_enhancement_rate": _safe_ratio(
            status_not_enhanced, candidate_count
        ),
    }


def evaluate_corpus(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    baseline = {case["id"]: baseline_decision(case) for case in cases}
    prototype = {case["id"]: prototype_decision(case) for case in cases}
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": validate_corpus(cases),
        "baseline": evaluate_predictions(cases, baseline),
        "prototype": evaluate_predictions(cases, prototype),
    }


def _live_prompt(batch: List[Dict[str, Any]]) -> str:
    payload = [
        {
            "id": case["id"],
            "source_type": case["source_type"],
            "source_text": case["source_text"],
        }
        for case in batch
    ]
    return (
        "请对每个输入只提取其主要事项，逐项返回，不遗漏 id。禁止把观察时间或文档修改时间"
        "当事项时间。evidence_quote 必须逐字复制输入中的最小支持片段。event_date 仅在事项"
        "明确发生、完成、状态变化或指标统计周期有日期时填写 YYYY-MM-DD，否则为 null。"
        "同一句明确写某日完成时 date_role=completion，某日决定/发生时为 event，某日进入阻塞/"
        "取消时为 status_change；observation 只表示来源明确说某日查看/采集，不是普通正文日期。"
        "date_role 只能是 event/completion/status_change/statistical_period/revision/publication/"
        "deadline/goal_horizon/historical_reference/observation/unknown。claim_kind 只能是"
        "progress_event/status_change/decision/metric/goal/forecast/current_status/background。"
        "modality 只能是 actual/observed/planned/target/forecast/unknown。status 只能是"
        "unknown/planned/in_progress/blocked/cancelled/completed。time_basis 只能是"
        "explicit_text/structural_context/relative_to_source_anchor/observation_time/unknown/conflict。"
        "来源冲突且无法消解时 has_conflict=true。只输出 JSON："
        '{"items":[{"id":"...","claim_text":"...","evidence_quote":"...",'
        '"claim_kind":"...","modality":"...","status":"...",'
        '"event_date":null,"date_role":"unknown","time_basis":"unknown",'
        '"has_conflict":false}]}。\n\n输入：' + json.dumps(payload, ensure_ascii=False)
    )


def _extract_json_payload(content: str) -> Dict[str, Any]:
    """兼容本地模型在 JSON format 下仍包裹 Markdown fence 的响应。"""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


def _ollama_batch(
    batch: List[Dict[str, Any]], model: str, base_url: str, timeout: int
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "claim_text",
            "evidence_quote",
            "claim_kind",
            "modality",
            "status",
            "event_date",
            "date_role",
            "time_basis",
            "has_conflict",
        ],
        "properties": {
            "id": {"type": "string"},
            "claim_text": {"type": "string"},
            "evidence_quote": {"type": "string"},
            "claim_kind": {
                "enum": [
                    "progress_event",
                    "status_change",
                    "decision",
                    "metric",
                    "goal",
                    "forecast",
                    "current_status",
                    "background",
                ]
            },
            "modality": {
                "enum": [
                    "actual",
                    "observed",
                    "planned",
                    "target",
                    "forecast",
                    "unknown",
                ]
            },
            "status": {
                "enum": [
                    "unknown",
                    "planned",
                    "in_progress",
                    "blocked",
                    "cancelled",
                    "completed",
                ]
            },
            "event_date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "date_role": {"enum": sorted(EVENT_DATE_ROLES | NON_EVENT_DATE_ROLES)},
            "time_basis": {
                "enum": [
                    "explicit_text",
                    "structural_context",
                    "relative_to_source_anchor",
                    "observation_time",
                    "unknown",
                    "conflict",
                ]
            },
            "has_conflict": {"type": "boolean"},
        },
    }
    body = {
        "model": model,
        "stream": False,
        "think": False,
        "format": {
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {"items": {"type": "array", "items": item_schema}},
        },
        "messages": [
            {
                "role": "system",
                "content": "你是工作记忆的事项时间标注器，只做忠实结构化提取。",
            },
            {"role": "user", "content": _live_prompt(batch)},
        ],
        "options": {
            "temperature": 0,
            "num_ctx": 8192,
            "num_predict": 2048,
            "repeat_penalty": 1.05,
        },
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = json.loads(response.read().decode("utf-8"))
    content = str((raw.get("message") or {}).get("content") or "")
    parsed = _extract_json_payload(content)
    usage = {
        "prompt_eval_count": int(raw.get("prompt_eval_count") or 0),
        "eval_count": int(raw.get("eval_count") or 0),
        "total_duration_ns": int(raw.get("total_duration") or 0),
    }
    return list(parsed.get("items") or []), usage


def live_evaluate(
    cases: List[Dict[str, Any]],
    model: str,
    base_url: str,
    batch_size: int,
    timeout: int,
    limit: int,
    checkpoint_path: Optional[Path] = None,
    evaluated_split: str = "all",
) -> Dict[str, Any]:
    selected = cases[:limit] if limit > 0 else list(cases)
    evaluation_cases = []
    for case in selected:
        normalized = dict(case)
        normalized["expected"] = dict(case.get("live_expected") or case["expected"])
        evaluation_cases.append(normalized)
    predictions = {}
    errors = []
    usage_total = Counter()
    completed_ids = set()
    if checkpoint_path and checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if (
            checkpoint.get("schema_version") == SCHEMA_VERSION
            and checkpoint.get("model_label") == "local_stage_a_model"
            and checkpoint.get("selected_count") == len(selected)
        ):
            predictions.update(checkpoint.get("predictions") or {})
            errors.extend(checkpoint.get("transport_errors") or [])
            # 失败批次不是完成状态；恢复运行时必须自适应缩小批量重试。
            failed_ids = {
                case_id
                for case_id, value in predictions.items()
                if value.get("reason") in {"model_batch_failed", "model_item_missing"}
            }
            for case_id in failed_ids:
                predictions.pop(case_id, None)
            errors = [item for item in errors if item.get("id") not in failed_ids]
            usage_total.update(checkpoint.get("usage") or {})
            completed_ids.update(predictions)
            if completed_ids and "successful_calls" not in usage_total:
                usage_total["successful_calls"] = int(
                    math.ceil(len(completed_ids) / float(batch_size))
                )
    started = time.monotonic()
    for offset in range(0, len(selected), batch_size):
        batch = [
            case
            for case in selected[offset : offset + batch_size]
            if case["id"] not in completed_ids
        ]
        if not batch:
            continue
        pending_batches = [batch]
        missing_attempts = Counter()
        while pending_batches:
            current_batch = pending_batches.pop(0)
            try:
                items, usage = _ollama_batch(current_batch, model, base_url, timeout)
                usage_total.update(usage)
                usage_total["successful_calls"] += 1
                by_id = {str(item.get("id") or ""): item for item in items}
                for case in current_batch:
                    candidate = by_id.get(case["id"])
                    if candidate is None:
                        missing_attempts[case["id"]] += 1
                        if missing_attempts[case["id"]] <= 1:
                            pending_batches.append([case])
                        else:
                            predictions[case["id"]] = {
                                "id": case["id"],
                                "decision": "unresolved",
                                "reason": "model_item_missing",
                            }
                            errors.append(
                                {"id": case["id"], "error": "model_item_missing"}
                            )
                    else:
                        predictions[case["id"]] = prototype_decision(case, candidate)
            except (
                urllib.error.URLError,
                TimeoutError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                usage_total["failed_calls"] += 1
                if len(current_batch) > 1:
                    midpoint = max(1, len(current_batch) // 2)
                    pending_batches.insert(0, current_batch[midpoint:])
                    pending_batches.insert(0, current_batch[:midpoint])
                    continue
                case = current_batch[0]
                predictions[case["id"]] = {
                    "id": case["id"],
                    "decision": "unresolved",
                    "reason": "model_batch_failed",
                }
                errors.append({"id": case["id"], "error": type(exc).__name__})
        completed_ids.update(case["id"] for case in batch)
        if checkpoint_path:
            _write_json(
                checkpoint_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "model_label": "local_stage_a_model",
                    "selected_count": len(selected),
                    "predictions": predictions,
                    "transport_errors": errors,
                    "usage": dict(usage_total),
                },
            )
        print(
            "live-evaluate progress: %d/%d" % (len(completed_ids), len(selected)),
            file=sys.stderr,
            flush=True,
        )
    elapsed = time.monotonic() - started
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "rule_version": RULE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_label": "local_stage_a_model",
        "selected_count": len(selected),
        "evaluated_split": evaluated_split,
        "batch_size": batch_size,
        "elapsed_seconds": round(elapsed, 3),
        "usage": dict(usage_total),
        "transport_errors": errors,
        "metrics": evaluate_predictions(evaluation_cases, predictions),
        "extraction_metrics": evaluate_extraction_semantics(
            evaluation_cases, predictions
        ),
        "predictions": predictions,
    }


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "evaluate", "live-evaluate"))
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default="qwen3.5:4b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--split", choices=("all", "development", "holdout"), default="all"
    )
    args = parser.parse_args()

    cases = load_corpus(args.corpus)
    if args.command == "validate":
        result = validate_corpus(cases)
    elif args.command == "evaluate":
        result = evaluate_corpus(cases)
    else:
        live_cases = (
            cases
            if args.split == "all"
            else [case for case in cases if case.get("split") == args.split]
        )
        result = live_evaluate(
            live_cases,
            args.model,
            args.base_url,
            max(1, args.batch_size),
            args.timeout,
            args.limit,
            args.output.with_suffix(".checkpoint.json") if args.output else None,
            args.split,
        )
    if args.output:
        _write_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("valid", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
