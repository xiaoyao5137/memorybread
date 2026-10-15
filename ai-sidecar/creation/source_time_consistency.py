"""Audit whether a generated metric keeps its source's statistical date.

The audit reads verified data results and generated text. It never rewrites the
document or treats a page collection timestamp as a statistical period. A
finding is a request for revision or review, not proof that a metric is false.
"""

import re
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SOURCE_TIME_AUDIT_VERSION = "memorybread.source-time-consistency.v1"
CONFIRMED_PERIOD_AUDIT_VERSION = "memorybread.confirmed-report-period.v1"

_DATE = re.compile(
    r"(?<!\d)(20\d{2})(?:[-/.]|年)(0?[1-9]|1[0-2])"
    r"(?:[-/.]|月)(0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_URL = re.compile(r"https?://[^\s),，。；）】>]+", re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+")
_CAPTURE_BEFORE = re.compile(
    r"(?:采集|抓取|获取|截取|页面刷新|页面访问)(?:时间|日期|日)?"
    r"\s*(?:为|是|于|在|[:：])?\s*$"
)
_CAPTURE_AFTER = re.compile(r"^\s*(?:采集|抓取|获取|截取|页面刷新|页面访问)(?:于|日|时间|日期)?")
_REQUEST_BEFORE = re.compile(
    r"(?:目标|请求|预期|用户要求|筛选|查询|报告)(?:统计)?"
    r"(?:周期|日期|时间|日|期|范围)?\s*(?:为|是|到|至|[:：])?\s*$"
)
_REQUEST_AFTER = re.compile(
    r"^\s*(?:目标|请求|预期|用户要求|筛选|查询|报告)(?:统计)?"
    r"(?:周期|日期|时间|日|期|范围)?"
)
_PERIOD_BEFORE = re.compile(
    r"(?:统计(?:周期|期间|日期|时间|日)?|数据(?:周期|日期|期)|"
    r"报表(?:周期|日期|期)|截至|截止|周期)\s*(?:为|是|到|至|[:：])?\s*$"
)
_PERIOD_AFTER = re.compile(
    r"^\s*(?:统计(?:周期|期间|日期|时间|日)?|数据(?:周期|日期|期)|"
    r"报表(?:周期|日期|期)|周期|当日(?:实时)?快照)"
)
_IMPLIED_SAME_DAY = re.compile(
    r"(?:统计(?:周期|日期|时间|日)|数据(?:周期|日期|期)|"
    r"报表(?:周期|日期|期))\s*(?:为|是|[:：])?\s*"
    r"(?:采集当日|当日|当天|实时快照|当日实时快照)"
)
_PERIOD_CONTEXT = re.compile(r"统计|数据期|报表期|周期|截至|截止|快照")
_CURRENT_SCOPE = re.compile(r"本周|当周|本期|当期|目标期|当前周期|目标周期|今日|本日")
_QUALIFIED_REFERENCE = re.compile(
    r"参考|历史|前期|上一|上周|上期|上月|去年|昨日|前日|仅供|仅作|"
    r"非本期|非本周|不能视为|不可视为|"
    r"尚未覆盖|未取得|未获得|暂用|缺失|不代表|不等于"
)
_REPORT_TARGET_ANCHOR = re.compile(
    r"(?:本次|已确认|用户确认|目标|当前)(?:的)?(?:报告|统计)?(?:周期|期|周|时间范围)"
    r"|(?:报告|周报)(?:统计)?(?:周期|期|范围)|本周|当周|本期"
)
_REPORT_TITLE = re.compile(r"周报|月报|季报|日报|报告")
_TITLE_NON_TARGET_DATE_BEFORE = re.compile(
    r"(?:来源|源页|原文|原始|历史|参考|对照)(?:的|中|所示)?"
    r"(?:页面)?(?:统计|数据|采集|抓取|获取|快照)?(?:周期|期|时间|日期|日)?"
    r"\s*(?:为|是|于|在|[:：])?\s*$"
    r"|(?:采集|抓取|获取|截取|页面刷新|页面访问|发布|成稿|交付|撰写|出刊)"
    r"(?:时间|日期|日|期)?"
    r"\s*(?:为|是|于|在|[:：])?\s*$"
)
_TITLE_NON_TARGET_DATE_AFTER = re.compile(
    r"^\s*[）)\]]?\s*(?:的)?(?:来源|源页|原文|原始|历史|参考|对照|采集|抓取|发布|成稿|交付|撰写|出刊)"
    r"(?:的|中|所示)?(?:统计|数据|采集|抓取|获取|快照)?"
    r"(?:周期|期|时间|日期|日)?"
)
_SOURCE_PERIOD_PREFIX = re.compile(r"(?:来源|源页|原文|原始|历史|参考|对照)(?:的|中|所示)?\s*$")
_OUTSIDE_TARGET = re.compile(
    r"(?:非|不是|不属于)(?:本次|目标|当前)?(?:报告|统计)?(?:周期|期|周)"
    r"|不在(?:本次|目标|当前)?(?:报告|统计)?(?:周期|期|周)(?:内|中)?"
    r"|(?:本次|目标|当前)?(?:报告|统计)?(?:周期|期|周)(?:以外|之外|外)"
)
_SHORT_DATE = re.compile(
    r"(?<!\d)(0?[1-9]|1[0-2])(?:[-/.]|月)(0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_RANGE_JOINER = re.compile(r"^\s*[）)\]]?\s*(?:至|到|—|–|～|~|－|-)\s*[（(\[]?\s*$")
_WEEK_NUMBER = re.compile(r"第\s*(\d{1,2})\s*周")


def _parsed_dates(text: str) -> List[Tuple[date, int, int]]:
    found: List[Tuple[date, int, int]] = []
    url_spans = [match.span() for match in _URL.finditer(text)]
    for match in _DATE.finditer(text):
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        try:
            value = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        found.append((value, match.start(), match.end()))
    return found


def _period(value: Any) -> Optional[Tuple[date, date]]:
    dates = [item[0] for item in _parsed_dates(str(value or ""))]
    if not dates:
        return None
    start, end = dates[0], dates[-1]
    return (start, end) if start <= end else None


def _capture_date(value: Any, timezone: ZoneInfo) -> Optional[date]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            seconds = float(value) / (1000 if abs(value) > 10_000_000_000 else 1)
            return datetime.fromtimestamp(seconds, timezone).date()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        parsed = _period(value)
        if parsed is not None:
            return parsed[0]
    return None


def _plain(text: Any) -> str:
    return re.sub(r"[\s*,，:：|\[\]（）()<>_\-]", "", str(text or "")).lower()


def _label_matches(label: str, line: str) -> bool:
    normalized_label = _plain(label)
    normalized_line = _plain(line)
    if not normalized_label or not normalized_line:
        return False
    common = SequenceMatcher(None, normalized_label, normalized_line, autojunk=False).find_longest_match(
        0, len(normalized_label), 0, len(normalized_line)
    )
    return common.size >= min(4, len(normalized_label))


def _verified_claims(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence = result.get("creation_evidence") or {}
    validation = (evidence.get("validation") or {}) if isinstance(evidence, dict) else {}
    structured = result.get("structured_data") or {}
    groups = [
        validation.get("verified_claims") if isinstance(validation, dict) else None,
        structured.get("verified_claims") if isinstance(structured, dict) else None,
    ]
    claims: List[Dict[str, Any]] = []
    seen = set()
    for group in groups:
        for claim in group or []:
            if not isinstance(claim, dict):
                continue
            key = (
                str(claim.get("label") or ""),
                str(claim.get("value") or ""),
                str(claim.get("statistical_period") or ""),
                str(claim.get("requested_metric") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            claims.append(claim)
    raw_risks = result.get("data_risks") or (
        validation.get("data_risks") if isinstance(validation, dict) else None
    ) or []
    period_risks = {}
    for risk in raw_risks:
        if not isinstance(risk, dict) or risk.get("kind") != "period_mismatch":
            continue
        key = (_plain(risk.get("label")), _plain(risk.get("value")))
        if key[0] and key[1] and _period(risk.get("actual_period")):
            period_risks[key] = risk
    normalized_claims = []
    for claim in claims:
        item = dict(claim)
        risk = period_risks.get((_plain(item.get("label")), _plain(item.get("value"))))
        if risk is not None:
            item["_period_mismatch_risk"] = risk
            if not _period(item.get("statistical_period")):
                item["statistical_period"] = risk["actual_period"]
        normalized_claims.append(item)
    return normalized_claims


def _sources(data_results: Sequence[Dict[str, Any]], timezone: ZoneInfo) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = []
    for result in data_results:
        if not isinstance(result, dict) or result.get("can_use") is not True:
            continue
        evidence = result.get("creation_evidence") or {}
        if result.get("source_kind") == "report_url" and (
            not isinstance(evidence, dict) or evidence.get("validation_status") != "verified"
        ):
            continue
        claims = _verified_claims(result)
        if not claims and not isinstance(result.get("structured_data"), dict):
            continue
        provenance = result.get("provenance") or {}
        if not isinstance(provenance, dict):
            provenance = {}
        title = str(result.get("title") or evidence.get("page_title") or "").strip()
        source_url = str(result.get("source_url") or evidence.get("source_url") or "").strip()
        capture = _capture_date(
            evidence.get("captured_at") or provenance.get("captured_at")
            or result.get("collected_at"),
            timezone,
        )
        sources.append({
            "source_id": result.get("source_id"),
            "title": title,
            "source_url": source_url,
            "capture_date": capture,
            "claims": claims,
        })
    return sources


def _source_anchor(line: str, source: Dict[str, Any]) -> bool:
    url = source["source_url"]
    title = source["title"]
    return bool((url and url in line) or (title and _plain(title) in _plain(line)))


def _matching_claims(line: str, source: Dict[str, Any]) -> List[Dict[str, Any]]:
    plain_line = _plain(line)
    matches = []
    for claim in source["claims"]:
        value = _plain(claim.get("value"))
        if len(value) < 3 or value not in plain_line:
            continue
        if not _label_matches(str(claim.get("label") or ""), line):
            continue
        matches.append(claim)
    return matches


def _date_role(line: str, start: int, end: int, previous_end: int, next_start: int) -> str:
    left = line[max(previous_end, start - 32):start]
    right = line[end:min(next_start, end + 16)]
    capture_before = _CAPTURE_BEFORE.search(left)
    capture_after = _CAPTURE_AFTER.search(right)
    request_before = _REQUEST_BEFORE.search(left)
    request_after = _REQUEST_AFTER.search(right)
    period_before = _PERIOD_BEFORE.search(left)
    period_after = _PERIOD_AFTER.search(right)
    candidates = []
    for role, match, is_before in (
        ("capture", capture_before, True),
        ("capture", capture_after, False),
        ("request", request_before, True),
        ("request", request_after, False),
        ("period", period_before, True),
        ("period", period_after, False),
    ):
        if match is not None:
            distance = len(left) - match.end() if is_before else match.start()
            candidates.append((distance, role))
    if not candidates:
        return "unknown"
    return min(candidates, key=lambda candidate: candidate[0])[1]


def _report_line_dates(text: str, target: Tuple[date, date]) -> List[Tuple[date, int, int]]:
    """Read explicit and short month/day dates in a report-period clause."""
    found = list(_parsed_dates(text))
    for match in _SHORT_DATE.finditer(text):
        if any(start <= match.start() < end for _, start, end in found):
            continue
        month = int(match.group(1))
        year = (target[1].year if target[0].year != target[1].year
                and month < target[0].month else target[0].year)
        try:
            parsed = date(year, month, int(match.group(2)))
        except ValueError:
            continue
        found.append((parsed, match.start(), match.end()))
    return sorted(found, key=lambda item: item[1])


def audit_confirmed_report_period(document: str, confirmed_period: Any) -> Dict[str, Any]:
    """Check assertions about a user-confirmed report period before delivery.

    A source's statistical date, collection date, or qualified historical
    comparison is not a claim about the document's target period. The guard
    returns revision findings; it never edits a protected chapter or guesses a
    period from prose.
    """
    result: Dict[str, Any] = {
        "schema_version": CONFIRMED_PERIOD_AUDIT_VERSION,
        "status": "not_applicable", "confirmed_period": None, "issues": [],
    }
    if (not isinstance(confirmed_period, dict)
            or confirmed_period.get("source") not in {"user", "user_edit", "confirmed_selection"}):
        return result
    try:
        start = date.fromisoformat(str(confirmed_period.get("start") or ""))
        end = date.fromisoformat(str(confirmed_period.get("end") or ""))
    except ValueError:
        result["status"] = "diagnostic"
        return result
    if start > end:
        result["status"] = "diagnostic"
        return result
    target = (start, end)
    result["status"] = "pass"
    result["confirmed_period"] = {"start": start.isoformat(), "end": end.isoformat()}
    if not document.strip():
        return result
    seen = set()

    def add_period_conflict(line_number: int, line: str,
                            claimed: Tuple[date, date]) -> None:
        key = (line_number, "report_target_period_conflict", claimed)
        if key in seen:
            return
        seen.add(key)
        result["issues"].append({
            "code": "report_target_period_conflict",
            "line_number": line_number,
            "claimed_period": {"start": claimed[0].isoformat(), "end": claimed[1].isoformat()},
            "confirmed_period": result["confirmed_period"],
            "excerpt": line.strip()[:260],
            "suggestion": "将本次报告周期统一为 {} 至 {}；来源统计期与采集日另行注明。".format(
                start.isoformat(), end.isoformat()),
        })

    for line_number, line in enumerate(document.splitlines(), 1):
        target_week = start.isocalendar()
        for week_match in _WEEK_NUMBER.finditer(line):
            week = int(week_match.group(1))
            before = line[max(0, week_match.start() - 32):week_match.start()]
            if (week == target_week.week or not re.search(r"(?:统计|报告|本次|本周)(?:周期|期|周)?", before)
                    or re.search(r"(?:来源|历史|参考)(?:页|资料|数据)?(?:的)?(?:统计|报告)?(?:周期|期|周)?\s*(?:为|是|[:：])?\s*$", before)):
                continue
            try:
                claimed_start = date.fromisocalendar(target_week.year, week, 1)
            except ValueError:
                continue
            key = (line_number, "report_week_number_conflict", week)
            if key not in seen:
                seen.add(key)
                result["issues"].append({
                    "code": "report_week_number_conflict", "line_number": line_number,
                    "claimed_period": {"start": claimed_start.isoformat(),
                                       "end": (claimed_start + timedelta(days=6)).isoformat()},
                    "confirmed_period": result["confirmed_period"],
                    "excerpt": line.strip()[:260],
                    "suggestion": "本次报告的 ISO 周次应为第 {} 周；来源周次须单独标为历史或参考。".format(target_week.week),
                })
        dates = _report_line_dates(line, target)
        if not dates:
            continue
        heading = _HEADING.match(line)
        if heading is not None and len(heading.group(1)) == 1:
            if len(dates) == 1 and _REPORT_TITLE.search(line):
                only = dates[0]
                before = line[max(0, only[1] - 32):only[1]]
                after = line[only[2]:only[2] + 24]
                if (not _TITLE_NON_TARGET_DATE_BEFORE.search(before)
                        and not _TITLE_NON_TARGET_DATE_AFTER.search(after)
                        and not start <= only[0] <= end):
                    add_period_conflict(line_number, line, (only[0], only[0]))
            for first, second in zip(dates, dates[1:]):
                if not _RANGE_JOINER.fullmatch(line[first[2]:second[1]]):
                    continue
                if not (_REPORT_TITLE.search(line) or _REPORT_TARGET_ANCHOR.search(line)
                        or first[1] - heading.end() <= 4):
                    continue
                before = line[max(0, first[1] - 32):first[1]]
                after = line[second[2]:second[2] + 24]
                if (_TITLE_NON_TARGET_DATE_BEFORE.search(before)
                        or _TITLE_NON_TARGET_DATE_AFTER.search(after)):
                    continue
                claimed = (first[0], second[0])
                if claimed != target:
                    add_period_conflict(line_number, line, claimed)
        for anchor in _REPORT_TARGET_ANCHOR.finditer(line):
            if _SOURCE_PERIOD_PREFIX.search(line[max(0, anchor.start() - 12):anchor.start()]):
                continue
            clause_end = re.search(r"[，,。；;]", line[anchor.end():])
            end_index = (anchor.end() + clause_end.start()) if clause_end else len(line)
            following = [item for item in dates if anchor.end() <= item[1] < end_index
                         and item[1] - anchor.end() <= 48]
            if not following:
                continue
            first = following[0]
            if (_CAPTURE_BEFORE.search(line[anchor.end():first[1]])
                    or _TITLE_NON_TARGET_DATE_BEFORE.search(line[anchor.end():first[1]])):
                continue
            second = next((item for item in following[1:]
                           if item[1] - first[2] <= 18
                           and _RANGE_JOINER.fullmatch(line[first[2]:item[1]])), None)
            claimed = (first[0], second[0]) if second else (first[0], first[0])
            conflict = claimed != target if second else not (start <= first[0] <= end)
            if not conflict:
                continue
            add_period_conflict(line_number, line, claimed)
        for outside in _OUTSIDE_TARGET.finditer(line):
            prior = [item for item in dates if item[2] <= outside.start()
                     and outside.start() - item[2] <= 28]
            if not prior:
                continue
            source_day = prior[-1]
            if not (start <= source_day[0] <= end):
                continue
            prior_end = dates[dates.index(source_day) - 1][2] if dates.index(source_day) else 0
            next_index = dates.index(source_day) + 1
            next_start = dates[next_index][1] if next_index < len(dates) else len(line)
            if _date_role(line, source_day[1], source_day[2], prior_end, next_start) == "capture":
                continue
            key = (line_number, "in_period_date_marked_outside", source_day[0])
            if key in seen:
                continue
            seen.add(key)
            result["issues"].append({
                "code": "in_period_date_marked_outside",
                "line_number": line_number,
                "claimed_period": {"start": source_day[0].isoformat(),
                                   "end": source_day[0].isoformat()},
                "confirmed_period": result["confirmed_period"],
                "excerpt": line.strip()[:260],
                "suggestion": "{} 在已确认报告周期内；如来源只覆盖单日，请说明它不是整周汇总。".format(
                    source_day[0].isoformat()),
            })
    if result["issues"]:
        result["status"] = "revise"
    return result


def _periods_for_line(source: Dict[str, Any], matched_claims: List[Dict[str, Any]]) -> List[Tuple[date, date]]:
    relevant = matched_claims if matched_claims else source["claims"]
    periods = {_period(claim.get("statistical_period")) for claim in relevant}
    periods.discard(None)
    return sorted(periods)


def _suggestion(source: Dict[str, Any], periods: List[Tuple[date, date]]) -> str:
    if periods:
        labels = [start.isoformat() if start == end else "{} 至 {}".format(start, end)
                  for start, end in periods]
        return "按来源统计期 {} 标注指标；采集日仅标为采集日。".format("、".join(labels))
    return "删除未经来源证实的统计日或实时快照表述；若只知道采集日，应单独标注采集日和统计期未注明。"


def _table_cells(line: str) -> List[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _table_separator(cells: List[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _number_tokens(value: Any) -> set:
    return {
        match.group(0).replace(",", "")
        for match in re.finditer(r"(?<!\d)[+-]?\d[\d,]*(?:\.\d+)?(?!\d)", str(value or ""))
    }


def _value_unit(value: Any) -> str:
    match = re.search(r"[+-]?\d[\d,]*(?:\.\d+)?\s*([^\d\s]+)", str(value or ""))
    return _plain(match.group(1)) if match is not None else ""


def _audit_current_value_headers(
    lines: List[str], sources: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Check the asserted period of a Markdown value column as a whole.

    A row's caveat does not undo a contradictory current-period column label.
    Matching requires the risk's metric label, numeric value, and unit carried
    by either the row or the column header.
    """
    issues: List[Dict[str, Any]] = []
    index = 0
    while index + 2 < len(lines):
        header = _table_cells(lines[index])
        separator = _table_cells(lines[index + 1])
        if not header or len(separator) != len(header) or not _table_separator(separator):
            index += 1
            continue
        end = index + 2
        while end < len(lines) and len(_table_cells(lines[end])) == len(header):
            end += 1
        current_columns = [
            column for column, cell in enumerate(header)
            if _CURRENT_SCOPE.search(cell) and not _QUALIFIED_REFERENCE.search(cell)
        ]
        if current_columns:
            for source in sources:
                matched_by_column: Dict[int, List[Dict[str, Any]]] = {}
                ambiguous_claims: List[Dict[str, Any]] = []
                for claim in source["claims"]:
                    if not claim.get("_period_mismatch_risk"):
                        continue
                    label = _plain(claim.get("label"))
                    value_tokens = _number_tokens(claim.get("value"))
                    unit = _value_unit(claim.get("value"))
                    if not label or not value_tokens:
                        continue
                    competing_sources = [
                        other for other in sources if other is not source
                        and any(
                            _plain(other_claim.get("label")) == label
                            and _number_tokens(other_claim.get("value")) == value_tokens
                            and _value_unit(other_claim.get("value")) == unit
                            for other_claim in other["claims"]
                        )
                    ]
                    claim_columns = set()
                    for row_index in range(index + 2, end):
                        row = _table_cells(lines[row_index])
                        if label not in _plain(" ".join(row)):
                            continue
                        claim_columns.update(
                            column for column in current_columns
                            if value_tokens & _number_tokens(row[column])
                            and (not unit or unit in _plain(header[column] + row[column]))
                        )
                    if len(claim_columns) == 1 and not competing_sources:
                        column = next(iter(claim_columns))
                        matched_by_column.setdefault(column, []).append(claim)
                    elif claim_columns:
                        # A repeated value does not prove which column carries
                        # the source claim. Keep the finding for review, but do
                        # not offer an automatic header rewrite.
                        ambiguous_claims.append(claim)
                column_matches = sorted(matched_by_column.items())
                if ambiguous_claims:
                    column_matches.append((None, ambiguous_claims))
                for column, matched in column_matches:
                    periods = _periods_for_line(source, matched)
                    capture = source["capture_date"]
                    issues.append({
                        "code": "qualified_period_in_current_value_column",
                        "source_id": source["source_id"],
                        "line_number": index + 1,
                        "column_index": column,
                        "column_indices": [column] if column is not None else [],
                        "claimed_statistical_date": None,
                        "source_statistical_periods": [
                            start.isoformat() if start == period_end else "{} 至 {}".format(start, period_end)
                            for start, period_end in periods
                        ],
                        "capture_date": capture.isoformat() if capture else None,
                        "metric_labels": sorted({str(item.get("label") or "") for item in matched}),
                        "excerpt": lines[index].strip()[:260],
                        "suggestion": (
                            "将当前周期数值列改为来源页显示值或历史参考值，"
                            "并在表头或行内注明实际统计日及目标周期缺口。"
                        ),
                    })
        index = end
    return issues


def source_period_coverage(
    data_results: Sequence[Dict[str, Any]],
    source_ids: Sequence[Any],
    time_context: Dict[str, Any],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> Dict[str, Any]:
    """Summarize verified metric periods against a caller-supplied target period.

    ``matched`` requires the entire source statistical period to lie within
    the target. A period that only overlaps, is broader than the target, or is
    not stated remains ``unknown``; a disjoint period is ``outside``. The
    caller must establish the target from an explicit user instruction, a
    frozen Skill requirement, or a confirmed brief, and retain that provenance.
    A provisional Skill target can guard against false current-period claims;
    it does not become a user-confirmed report period through this function.
    """
    result: Dict[str, Any] = {
        "schema_version": SOURCE_TIME_AUDIT_VERSION,
        "status": "ok",
        "target_period": None,
        "source_counts": {"matched": 0, "outside": 0, "unknown": 0, "mixed": 0},
        "claim_counts": {"matched": 0, "outside": 0, "unknown": 0},
        "sources": [],
        "diagnostics": [],
    }
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        result["status"] = "diagnostic"
        result["diagnostics"].append({"reason": "invalid_timezone", "timezone": timezone_name})
        return result
    if not isinstance(time_context, dict):
        time_context = {}
    start_span = _period(time_context.get("period_start") or time_context.get("start"))
    end_span = _period(time_context.get("period_end") or time_context.get("end"))
    target_start = start_span[0] if start_span is not None else None
    target_end = end_span[1] if end_span is not None else None
    if target_start is None or target_end is None or target_start > target_end:
        result["status"] = "diagnostic"
        result["diagnostics"].append({"reason": "target_period_invalid"})
        return result
    result["target_period"] = {
        "start": target_start.isoformat(),
        "end": target_end.isoformat(),
    }
    source_map = {str(source["source_id"]): source for source in _sources(data_results, timezone)}
    requested_ids = list(dict.fromkeys(str(source_id) for source_id in source_ids))
    for source_id in requested_ids:
        source = source_map.get(source_id)
        counts = {"matched": 0, "outside": 0, "unknown": 0}
        claims_summary = []
        if source is None:
            status = "unknown"
            result["diagnostics"].append({
                "reason": "source_missing_or_unverified",
                "source_id": source_id,
            })
        else:
            for claim in source["claims"]:
                span = _period(claim.get("statistical_period"))
                if span is None:
                    classification = "unknown"
                    reason = "statistical_period_unstated"
                elif target_start <= span[0] and span[1] <= target_end:
                    classification = "matched"
                    reason = "statistical_period_within_target"
                elif span[1] < target_start or span[0] > target_end:
                    classification = "outside"
                    reason = "statistical_period_disjoint"
                else:
                    classification = "unknown"
                    reason = "statistical_period_partially_overlaps_target"
                counts[classification] += 1
                result["claim_counts"][classification] += 1
                claims_summary.append({
                    "label": str(claim.get("label") or ""),
                    "requested_metric": str(claim.get("requested_metric") or ""),
                    "statistical_period": str(claim.get("statistical_period") or ""),
                    "status": classification,
                    "exact_target_range": bool(span is not None
                                               and span[0] == target_start
                                               and span[1] == target_end),
                    "reason": reason,
                })
            if not claims_summary:
                status = "unknown"
                result["diagnostics"].append({
                    "reason": "source_has_no_verified_claims",
                    "source_id": source_id,
                })
            else:
                occupied = [kind for kind, count in counts.items() if count]
                status = occupied[0] if len(occupied) == 1 else "mixed"
        result["source_counts"][status] += 1
        result["sources"].append({
            "source_id": source["source_id"] if source is not None else source_id,
            "status": status,
            "claim_counts": counts,
            "claims": claims_summary,
        })
    return result


def audit_source_time_consistency(
    document: str,
    data_results: Sequence[Dict[str, Any]],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> Dict[str, Any]:
    """Return source/date findings without mutating the document or evidence.

    Only a verified source with a unique metric match, an explicit citation, or
    a unique citation in the same Markdown section can be audited. A bare
    document date and a correctly labeled collection date are not treated as
    source statistical dates.
    """
    result: Dict[str, Any] = {
        "schema_version": SOURCE_TIME_AUDIT_VERSION,
        "status": "pass",
        "issues": [],
        "diagnostics": [],
    }
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        result["status"] = "diagnostic"
        result["diagnostics"].append({"reason": "invalid_timezone", "timezone": timezone_name})
        return result
    if not document.strip():
        return result
    sources = _sources(data_results, timezone)
    if not sources:
        result["status"] = "diagnostic"
        result["diagnostics"].append({"reason": "no_verified_data_sources"})
        return result

    lines = document.splitlines()
    section_ids: List[int] = []
    section_id = 0
    section_headings: Dict[int, str] = {}
    for line in lines:
        heading = _HEADING.match(line)
        if heading and len(heading.group(1)) <= 3:
            section_id += 1
            section_headings[section_id] = line
        section_ids.append(section_id)
    anchors: Dict[int, List[int]] = {}
    for index, line in enumerate(lines):
        for source_index, source in enumerate(sources):
            if _source_anchor(line, source):
                anchors.setdefault(section_ids[index], []).append(source_index)

    seen = set()
    for line_number, line in enumerate(lines, 1):
        if _HEADING.match(line):
            continue
        dates = _parsed_dates(line)
        implied = bool(_IMPLIED_SAME_DAY.search(line))
        if (
            not dates and not implied
            and not _CURRENT_SCOPE.search(line)
            and not _CURRENT_SCOPE.search(section_headings.get(section_ids[line_number - 1], ""))
        ):
            continue
        direct = [i for i, source in enumerate(sources) if _source_anchor(line, source)]
        claim_matches = {
            i: _matching_claims(line, source)
            for i, source in enumerate(sources)
        }
        claim_matches = {i: claims for i, claims in claim_matches.items() if claims}
        if direct:
            candidate_indices = direct
        elif claim_matches:
            candidate_indices = list(claim_matches)
        else:
            section_anchors = set(anchors.get(section_ids[line_number - 1], []))
            candidate_indices = list(section_anchors) if len(section_anchors) == 1 else []
        if len(candidate_indices) != 1:
            continue
        source = sources[candidate_indices[0]]
        matched = claim_matches.get(candidate_indices[0], [])
        # Section-only binding needs an explicit period assertion. This avoids
        # treating a report date near a citation as a metric statistical date.
        if not direct and not matched and not _PERIOD_CONTEXT.search(line):
            continue
        periods = _periods_for_line(source, matched)
        if not matched and len(periods) > 1:
            result["diagnostics"].append({
                "reason": "multiple_source_periods_without_metric_binding",
                "source_id": source["source_id"],
                "line_number": line_number,
            })
            continue
        capture = source["capture_date"]
        stat_dates = []
        for date_index, (claimed, start, end) in enumerate(dates):
            previous_end = dates[date_index - 1][2] if date_index else 0
            next_start = dates[date_index + 1][1] if date_index + 1 < len(dates) else len(line)
            if _date_role(line, start, end, previous_end, next_start) == "period":
                stat_dates.append(claimed)
        for claimed in stat_dates:
            if periods and any(start <= claimed <= end for start, end in periods):
                continue
            if periods:
                code = "statistical_period_conflict"
            elif capture == claimed:
                code = "capture_date_promoted_to_statistical_period"
            else:
                code = "statistical_period_unverified"
            key = (source["source_id"], line_number, code, claimed.isoformat())
            if key in seen:
                continue
            seen.add(key)
            result["issues"].append({
                "code": code,
                "source_id": source["source_id"],
                "line_number": line_number,
                "claimed_statistical_date": claimed.isoformat(),
                "source_statistical_periods": [
                    start.isoformat() if start == end else "{} 至 {}".format(start, end)
                    for start, end in periods
                ],
                "capture_date": capture.isoformat() if capture else None,
                "metric_labels": sorted({str(item.get("label") or "") for item in matched}),
                "excerpt": line.strip()[:260],
                "suggestion": _suggestion(source, periods),
            })
        risky_claims = [item for item in matched if item.get("_period_mismatch_risk")]
        current_scope = bool(
            _CURRENT_SCOPE.search(line)
            or _CURRENT_SCOPE.search(section_headings.get(section_ids[line_number - 1], ""))
        )
        if risky_claims and current_scope and not stat_dates and not _QUALIFIED_REFERENCE.search(line):
            risk_periods = sorted({
                str(item["_period_mismatch_risk"].get("actual_period") or "")
                for item in risky_claims
            })
            key = (source["source_id"], line_number, "qualified_period_presented_as_target")
            if key not in seen:
                seen.add(key)
                result["issues"].append({
                    "code": "qualified_period_presented_as_target",
                    "source_id": source["source_id"],
                    "line_number": line_number,
                    "claimed_statistical_date": None,
                    "source_statistical_periods": [
                        start.isoformat() if start == end else "{} 至 {}".format(start, end)
                        for start, end in periods
                    ],
                    "capture_date": capture.isoformat() if capture else None,
                    "metric_labels": sorted({str(item.get("label") or "") for item in risky_claims}),
                    "excerpt": line.strip()[:260],
                    "suggestion": (
                        "明确标注实际统计期 {}，并说明它仅为目标周期的参考值。"
                        .format("、".join(risk_periods))
                    ),
                })
        if implied and not stat_dates and any(
            _date_role(line, start, end,
                       dates[index - 1][2] if index else 0,
                       dates[index + 1][1] if index + 1 < len(dates) else len(line)) == "capture"
            for index, (_, start, end) in enumerate(dates)
        ):
            key = (source["source_id"], line_number, "capture_date_implied_as_statistical_period")
            if key not in seen:
                seen.add(key)
                result["issues"].append({
                    "code": "capture_date_implied_as_statistical_period",
                    "source_id": source["source_id"],
                    "line_number": line_number,
                    "claimed_statistical_date": None,
                    "source_statistical_periods": [
                        start.isoformat() if start == end else "{} 至 {}".format(start, end)
                        for start, end in periods
                    ],
                    "capture_date": capture.isoformat() if capture else None,
                    "metric_labels": sorted({str(item.get("label") or "") for item in matched}),
                    "excerpt": line.strip()[:260],
                    "suggestion": _suggestion(source, periods),
                })
    result["issues"].extend(_audit_current_value_headers(lines, sources))
    if result["issues"]:
        result["status"] = "review"
    elif result["diagnostics"]:
        result["status"] = "diagnostic"
    return result
