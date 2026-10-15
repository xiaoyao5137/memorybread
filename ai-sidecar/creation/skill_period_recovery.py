"""Conservative recovery plan for generated Skill steps with stale period facts.

This module only identifies generated step outputs that can be superseded. The
caller keeps the old output in the operation audit, replaces active output only
after a new step succeeds, and never changes a source snapshot or user document.
An observed/collection timestamp is deliberately not accepted as a fact date.
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple


PERIOD_RECOVERY_VERSION = "memorybread.skill-period-recovery.v1"

_ABSOLUTE_DATE = re.compile(
    r"(?<!\d)(20\d{2})(?:[-/.]|年)(0?[1-9]|1[0-2])"
    r"(?:[-/.]|月)(0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_CURRENT_SCOPE = re.compile(r"本周|当周|本期|this\s+week|current\s+week", re.IGNORECASE)
_HISTORICAL_SCOPE = re.compile(
    r"历史|旧资料|旧周期|上周|上月|去年|此前|前期|过往|对照|事实参考|"
    r"仅作参考|仅供参考|周期外|非本周|非本期|不作为本周|不作为本期|"
    r"未覆盖|未匹配|不能视为|不可视为|不得视为|尚无|未查到|未找到"
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+)$")


def _iso_date(value: Any) -> Optional[date]:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _dates_in_line(line: str) -> List[date]:
    parsed: List[date] = []
    for match in _ABSOLUTE_DATE.finditer(line):
        try:
            parsed.append(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            continue
    return parsed


def _stale_current_facts(
    content: str, period_start: date, period_end: date
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return only unqualified absolute dates asserted inside current sections.

    A relative date without a year, an old source mentioned as historical
    context, or a quoted data gap is not enough to remove an authored step.
    """
    stale: List[Dict[str, Any]] = []
    needs_review = False
    current_heading = ""
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.strip()
        heading = _HEADING.match(line)
        if heading is not None:
            current_heading = heading.group(1).strip()
        dates = _dates_in_line(line)
        if not dates:
            continue
        if not (_CURRENT_SCOPE.search(line) or _CURRENT_SCOPE.search(current_heading)):
            continue
        if _HISTORICAL_SCOPE.search(line) or _HISTORICAL_SCOPE.search(current_heading):
            needs_review = True
            continue
        for fact_date in dates:
            if period_start <= fact_date <= period_end:
                continue
            stale.append(
                {
                    "line_number": line_number,
                    "fact_date": fact_date.isoformat(),
                    "reason": "unqualified_out_of_period_fact_in_current_section",
                }
            )
    return stale, needs_review


def plan_skill_period_recovery(
    time_context: Dict[str, Any],
    frozen_steps: Sequence[Dict[str, Any]],
    completed_outputs: Sequence[Dict[str, Any]],
    *,
    target_confirmed: bool,
    chapter_base_document: str,
    persisted_document: str,
    user_revision_status: str,
) -> Dict[str, Any]:
    """Plan safe supersession without editing any checkpoint or document.

    ``target_confirmed`` must come from a user-confirmed instruction or brief,
    not from an execution-day default. ``user_revision_status`` is ``none`` only
    when no user-authored body/revision exists; ``present`` and ``unknown`` both
    prevent automatic supersession. The caller must archive the old outputs and
    keep retrieval/tool receipts before applying this plan.
    """
    result: Dict[str, Any] = {
        "schema_version": PERIOD_RECOVERY_VERSION,
        "status": "diagnostic",
        "supersede_step_ids": [],
        "rewrite_step_ids": [],
        "review_step_ids": [],
        "diagnostics": [],
    }
    if not target_confirmed:
        result["diagnostics"].append({"reason": "target_period_not_confirmed"})
        return result
    period_start = _iso_date(time_context.get("period_start")) if isinstance(time_context, dict) else None
    period_end = _iso_date(time_context.get("period_end")) if isinstance(time_context, dict) else None
    if period_start is None or period_end is None or period_start > period_end:
        result["diagnostics"].append({"reason": "target_period_invalid"})
        return result
    if chapter_base_document.strip() or persisted_document.strip():
        result["diagnostics"].append({"reason": "protected_document_present"})
        return result
    if user_revision_status != "none":
        result["diagnostics"].append({"reason": "user_revision_present_or_unknown"})
        return result

    frozen: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for step in frozen_steps:
        if not isinstance(step, dict) or step.get("action") != "skill_step":
            continue
        skill_id = str(step.get("skill_id") or "")
        step_id = str(step.get("skill_step_id") or "")
        if skill_id and step_id:
            frozen[(skill_id, step_id)] = step

    for output in completed_outputs:
        if not isinstance(output, dict):
            continue
        skill_id = str(output.get("skill_id") or "")
        step_id = str(output.get("step_id") or "")
        key = (skill_id, step_id)
        step = frozen.get(key)
        if step is None:
            result["diagnostics"].append(
                {"step_id": step_id, "reason": "frozen_step_not_found"}
            )
            continue
        objective = str(step.get("skill_step_objective") or "")
        if not _CURRENT_SCOPE.search(objective):
            continue
        if output.get("objective") and str(output["objective"]) != objective:
            result["diagnostics"].append(
                {"step_id": step_id, "reason": "frozen_objective_mismatch"}
            )
            continue
        content = str(output.get("content") or "")
        if not content.strip():
            result["diagnostics"].append(
                {"step_id": step_id, "reason": "completed_output_empty"}
            )
            continue
        stale, needs_review = _stale_current_facts(content, period_start, period_end)
        if stale:
            result["supersede_step_ids"].append(step_id)
            result["rewrite_step_ids"].append(step_id)
            result["diagnostics"].append(
                {"step_id": step_id, "reason": "stale_current_fact", "evidence": stale}
            )
        elif needs_review:
            result["review_step_ids"].append(step_id)
            result["diagnostics"].append(
                {"step_id": step_id, "reason": "historical_reference_requires_review"}
            )
        else:
            result["review_step_ids"].append(step_id)
            result["diagnostics"].append(
                {"step_id": step_id, "reason": "period_not_proven_by_step_output"}
            )
    if result["supersede_step_ids"]:
        result["status"] = "rewrite"
    return result
