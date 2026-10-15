"""Bounded revisions of generated claims contradicted by verified sources.

The caller supplies independent audit findings. This module only edits the
exact generated line or sentence those findings identify; the caller keeps the
original preview in its checkpoint before publishing the revision.
"""

import re
from typing import Any, Dict, List, Sequence, Tuple


_CURRENT_SCOPE = re.compile(r"本周|当周|本期|当期|目标期|当前周期|目标周期|今日|本日")
_REFERENCE_SCOPE = re.compile(r"参考|历史|前期|上一|上周|上期|上月|去年|昨日|前日|仅供|仅作|非本期|非本周")
_DATE_REVISION_CODES = {
    "statistical_period_conflict",
    "statistical_period_unverified",
    "capture_date_promoted_to_statistical_period",
    "capture_date_implied_as_statistical_period",
    "qualified_period_presented_as_target",
}


def _source_notice(issue: Dict[str, Any]) -> str:
    periods = list(dict.fromkeys(
        str(value).strip() for value in issue.get("source_statistical_periods") or []
        if str(value).strip()
    ))
    labels = list(dict.fromkeys(
        str(value).strip() for value in issue.get("metric_labels") or []
        if str(value).strip()
    ))
    subject = "「{}」".format("、".join(labels[:3])) if labels else "来源页指标"
    if periods:
        detail = "{}的来源统计期为 {}。".format(subject, "、".join(periods[:4]))
    else:
        capture = str(issue.get("capture_date") or "").strip()
        detail = "来源页面{}未注明指标统计期。".format(
            "采集于 {}，但".format(capture) if capture else ""
        )
    return "【来源周期待核验：{}目标周期内的值尚待核实。】".format(detail)


def _revise_date_claim(line: str, issue: Dict[str, Any]) -> str:
    """Retract one identifiable claim while preserving other facts on its line.

    The audit identifies a line, but a writer may put several independent
    sentences there. If the offending span is not unique, leave the line to
    delivery review rather than discard unrelated source-backed prose.
    """
    code = str(issue.get("code") or "")
    claimed_date = str(issue.get("claimed_statistical_date") or "")
    date_pattern = None
    date_parts = re.fullmatch(r"(20\d{2})-(\d{2})-(\d{2})", claimed_date)
    if date_parts:
        year, month, day = date_parts.groups()
        date_pattern = re.compile(
            r"{}(?:[-/.]|年)0?{}(?:[-/.]|月)0?{}日?".format(
                year, int(month), int(day)))
    spans = list(re.finditer(r"[^。！？!?；;]+[。！？!?；;]?", line))
    matches = []
    for span in spans:
        clause = span.group(0)
        if code == "capture_date_implied_as_statistical_period":
            selected = bool(re.search(
                r"(?:统计|数据|报表)(?:周期|日期|时间|日|期)?"
                r"\s*(?:为|是|[:：])?\s*(?:采集当日|当日|当天|实时快照|当日实时快照)",
                clause,
            ))
        elif code == "qualified_period_presented_as_target":
            labels = [str(item).strip() for item in issue.get("metric_labels") or []
                      if str(item).strip()]
            selected = bool(_CURRENT_SCOPE.search(clause) and labels
                            and any(label in clause for label in labels))
        else:
            selected = bool(date_pattern and date_pattern.search(clause)
                            and re.search(r"统计|数据|报表|截至|截止|周期|快照", clause))
        if selected:
            matches.append(span)
    if len(matches) != 1:
        return line
    span = matches[0]
    original = span.group(0)
    leading = original[:len(original) - len(original.lstrip())]
    return (line[:span.start()] + leading + _source_notice(issue)
            + line[span.end():])


def _qualified_header(line: str, issue: Dict[str, Any]) -> str:
    cells = line.split("|")
    if len(cells) < 3:
        return line
    columns = {value for value in issue.get("column_indices") or []
               if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
    if not columns:
        return line
    periods = list(dict.fromkeys(
        str(value).strip() for value in issue.get("source_statistical_periods") or []
        if str(value).strip()
    ))
    suffix = "统计期 {}".format("、".join(periods[:3])) if periods else "统计期未注明"
    revised = []
    for index, cell in enumerate(cells):
        if ((index - 1) in columns and _CURRENT_SCOPE.search(cell)
                and not _REFERENCE_SCOPE.search(cell)):
            revised.append(" 来源页显示值（{}） ".format(suffix))
        else:
            revised.append(cell)
    return "|".join(revised)


def revise_source_grounded_preview(
    document: str,
    source_time_issues: Sequence[Dict[str, Any]],
    ranked_row_conflicts: Sequence[Dict[str, Any]],
    *,
    protected_base: str = "",
) -> Tuple[str, List[Dict[str, Any]]]:
    """Retract only verified contradictions in the uncommitted chapter body.

    Every date replacement discloses the source's statistical period or the
    absence of one. No collection date is promoted to a statistical period.
    A rank correction is made only when its cited sentence is still present.
    Unmatched findings remain for the normal delivery review to reject.
    """
    if protected_base and not document.startswith(protected_base):
        return document, [{"status": "skipped", "reason": "protected_base_changed"}]
    lines = document.splitlines(keepends=True)
    starts = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    original_lines = list(lines)
    edits: List[Dict[str, Any]] = []
    applied_lines = set()
    header_lines = set()
    for issue in source_time_issues:
        if not isinstance(issue, dict):
            continue
        number = issue.get("line_number")
        if not isinstance(number, int) or number < 1 or number > len(lines):
            continue
        index = number - 1
        original = original_lines[index].rstrip("\r\n")
        current = lines[index].rstrip("\r\n")
        excerpt = str(issue.get("excerpt") or "").strip()
        code = str(issue.get("code") or "")
        if (index in applied_lines or starts[index] < len(protected_base)
                or not excerpt or not original.strip().startswith(excerpt)):
            continue
        if code == "qualified_period_in_current_value_column":
            replacement = _qualified_header(current, issue)
        else:
            if (index in header_lines or code not in _DATE_REVISION_CODES
                    or original.lstrip().startswith("|")
                    or re.match(r"^\s{0,3}#{1,6}\s+", original)):
                continue
            replacement = _revise_date_claim(current, issue)
        if replacement == current:
            continue
        ending = lines[index][len(current):]
        lines[index] = replacement + ending
        if code == "qualified_period_in_current_value_column":
            header_lines.add(index)
        else:
            applied_lines.add(index)
        edits.append({"kind": "source_time", "code": issue.get("code"),
                      "source_id": issue.get("source_id"), "line_number": number,
                      "before": current, "after": replacement})
    for conflict in ranked_row_conflicts:
        if not isinstance(conflict, dict):
            continue
        name = str(conflict.get("name") or "").strip()
        excerpt = str(conflict.get("excerpt") or "").strip()
        limit = conflict.get("limit")
        if not name or not excerpt or not isinstance(limit, int) or limit <= 0:
            continue
        number = conflict.get("line_number")
        indices = ([number - 1] if isinstance(number, int) and 1 <= number <= len(lines)
                   else range(len(lines)))
        for index in indices:
            line = lines[index]
            if (starts[index] < len(protected_base) or index in applied_lines
                    or index in header_lines):
                continue
            original = line.rstrip("\r\n")
            if excerpt not in original or original.lstrip().startswith("|"):
                continue
            replacement_sentence = "已验证的前 {} 行包含「{}」，此前排行判断已撤回".format(
                limit, name
            )
            end = original.index(excerpt) + len(excerpt)
            if end < len(original) and original[end] not in "。！？!?；;，,":
                # A truncated excerpt cannot establish a safe edit span.
                # Leave it for delivery review rather than discard the line.
                continue
            replacement = original.replace(excerpt, replacement_sentence, 1)
            ending = line[len(original):]
            lines[index] = replacement + ending
            applied_lines.add(index)
            edits.append({"kind": "rank_membership", "row_id": conflict.get("row_id"),
                          "relation_id": conflict.get("relation_id"),
                          "line_number": index + 1, "before": original,
                          "after": replacement})
            break
    return "".join(lines), edits
