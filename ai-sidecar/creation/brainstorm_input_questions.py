"""Plan source-aware input clarifications before creative detail questions.

The planner is pure. Its caller freezes ``as_of_date`` and the local preflight
snapshot in the Brainstorm session. A suggested option is never an answer.
Only a user-authored range or a structured, saved user answer resolves a period.
"""

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from .brainstorm_input_preflight import build_input_needs


_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?(?!\d)")
_SAME_YEAR_END = re.compile(
    r"^\s*(?:至|到|[-—–~～])\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?(?=$|[\s的，。])"
)
_RANGE_JOIN = re.compile(r"^\s*(?:至|到|[-—–~～])\s*$")
_ISO_WEEK = re.compile(r"(?<!\d)(\d{4})\s*年\s*第\s*(\d{1,2})\s*周(?!\d)")
_WEEKLY = re.compile(r"周报|周度(?:报告|总结)|weekly\s+(?:report|digest|update)", re.I)
_OTHER_CADENCE = re.compile(r"月报|月度(?:报告|总结)|日报|日度报告|季度(?:报告|总结)")
_NEED_STATUSES = frozenset({"matched", "candidate", "partial", "missing", "failed", "skipped", "pending"})
_TITLE_YEAR_MONTH = re.compile(
    r"(?<!\d)(20\d{2})\s*(?:年|[-/.])\s*(\d{1,2})\s*月?(?![-/.\d])")
_TITLE_MONTH = re.compile(r"(?<!\d)(\d{1,2})\s*月")


def _day(value: Union[str, date]) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("as_of_date must be an ISO calendar date")
    year, month, day = (int(part) for part in value.split("-"))
    return date(year, month, day)


def _range(start: date, end: date, timezone: str) -> Optional[Dict[str, str]]:
    if start > end or (end - start).days > 366 or not timezone:
        return None
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": timezone}


def _structured_range(value: Any, timezone: str) -> Optional[Dict[str, str]]:
    if not isinstance(value, dict):
        return None
    try:
        start = _day(value.get("start"))
        end = _day(value.get("end"))
    except (TypeError, ValueError):
        return None
    zone = value.get("timezone", timezone)
    return _range(start, end, zone) if isinstance(zone, str) else None


def _explicit_range(text: str, as_of: date, timezone: str,
                    *, allow_relative: bool) -> Optional[Dict[str, str]]:
    """Recognize clear user words only; never mine a Skill or recalled prose."""
    matches = list(_FULL_DATE.finditer(text))
    if len(matches) == 2 and _RANGE_JOIN.fullmatch(text[matches[0].end():matches[1].start()]):
        try:
            start = date(*(int(part) for part in matches[0].groups()))
            end = date(*(int(part) for part in matches[1].groups()))
        except ValueError:
            return None
        return _range(start, end, timezone)
    if len(matches) == 1:
        end_match = _SAME_YEAR_END.match(text[matches[0].end():])
        if end_match:
            try:
                start = date(*(int(part) for part in matches[0].groups()))
                end = date(start.year, int(end_match.group(1)), int(end_match.group(2)))
            except ValueError:
                return None
            return _range(start, end, timezone)
    weeks = list(_ISO_WEEK.finditer(text))
    if len(weeks) == 1 and not matches:
        try:
            start = date.fromisocalendar(int(weeks[0].group(1)), int(weeks[0].group(2)), 1)
        except ValueError:
            return None
        return _range(start, start + timedelta(days=6), timezone)
    if not allow_relative or matches or weeks:
        return None
    current = bool(re.search(r"本周|这周|这一周", text))
    previous = bool(re.search(r"上周|上一周", text))
    if current == previous:
        return None
    start = as_of - timedelta(days=as_of.weekday() + (7 if previous else 0))
    return _range(start, start + timedelta(days=6), timezone)


def _weekly_request(root_request: str, selected_skills: List[Dict[str, Any]]) -> bool:
    if _OTHER_CADENCE.search(root_request) and not _WEEKLY.search(root_request):
        return False
    if _WEEKLY.search(root_request):
        return True
    for skill in selected_skills:
        if not isinstance(skill, dict):
            continue
        description = skill.get("skillDescription", skill.get("skill_description", {}))
        document_types = description.get("documentTypes", description.get("document_types", [])) if isinstance(description, dict) else []
        if any(_WEEKLY.search(str(item)) for item in document_types):
            return True
        if _WEEKLY.search(str(skill.get("title", ""))):
            return True
    return False


def _skill_need_ids(selected_skills: List[Dict[str, Any]]) -> List[str]:
    return [need["id"] for need in build_input_needs(selected_skills)]


def _saved_user_period(decisions: List[Dict[str, Any]], as_of: date,
                       timezone: str) -> Tuple[bool, Optional[Dict[str, str]]]:
    """Use only a user answer to the dedicated clarification, never a prose hint."""
    found = False
    result = None
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        marker = decision.get("input_clarification")
        if not isinstance(marker, dict) or marker.get("kind") != "report_period":
            continue
        if decision.get("answer_source", decision.get("source")) != "user":
            continue
        found = True
        value = decision.get("input_value")
        if value is None:
            selected = decision.get("selected_options", [])
            if isinstance(selected, list) and len(selected) == 1 and isinstance(selected[0], dict):
                value = selected[0].get("input_value")
        result = _structured_range(value, timezone)
        if result is None:
            custom = decision.get("custom_text", decision.get("answer", ""))
            result = _explicit_range(str(custom), as_of, timezone, allow_relative=False)
    return found, result


def _stable_id(prefix: str, value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _label(start: date, end: date) -> str:
    return "{} 至 {}".format(start.isoformat(), end.isoformat())


def _period_question(root_request: str, as_of: date, timezone: str,
                     need_ids: List[str], candidate_ranges: Optional[List[Dict[str, Any]]] = None,
                     cadence: str = "weekly") -> Dict[str, Any]:
    current_start = as_of - timedelta(days=as_of.weekday())
    previous_start = current_start - timedelta(days=7)
    if candidate_ranges:
        periods = []
        names = {"latest_completed_week": "最近完整一周", "current_week": "本周",
                 "latest_completed_month": "最近完整一月", "current_month": "本月"}
        for item in candidate_ranges[:4]:
            if not isinstance(item, dict):
                continue
            value = _structured_range(item, timezone)
            if value:
                periods.append((str(item.get("kind", "candidate")),
                    names.get(str(item.get("kind")), "备选周期"),
                    _day(value["start"]), _day(value["end"])))
    else:
        periods = [("latest_completed_week", "最近完整一周", previous_start,
                    previous_start + timedelta(days=6)),
                   ("current_week", "本周", current_start, current_start + timedelta(days=6))]
    if len(periods) < 2:
        raise ValueError("period clarification needs two valid candidate ranges")
    options = []
    for identifier, name, start, end in periods:
        options.append({"id": identifier, "label": name + "（" + _label(start, end) + "）",
                        "description": "统计范围为 {} 至 {}；资料会按其实际统计期核对。".format(start.isoformat(), end.isoformat()),
                        "details": "候选周期尚未选定，也不表示该周期资料已齐全。",
                        "recommended": False,
                        "input_value": _range(start, end, timezone)})
    return {"id": _stable_id("input_report_period_", [root_request, as_of.isoformat(),
                timezone, need_ids, [(item[0], item[2].isoformat(), item[3].isoformat()) for item in periods]]),
            "parent_question_id": None, "parent_option_id": None,
            "exploration_stage": "explore", "dimension_id": "report_period", "dimension": "报告周期",
            "type": "single_choice", "single_choice_reason": "不同统计周期的范围互斥",
            "prompt": "这份{}应覆盖哪个周期？".format("周报" if cadence == "weekly" else "报告"),
            "why_now": "先确认统计起止日，再按实际时期核对所需资料与指标来源。",
            "context_details": "当前尚无用户确认的起止日；以下日期只是备选范围，不代表本地资料可用。",
            "required": True, "allow_custom": True,
            "answer_template": "例如：{} 至 {}（请写完整年份与起止日）".format(
                periods[0][2].isoformat(), periods[0][3].isoformat()),
            "options": options,
            "input_clarification": {"kind": "report_period", "need_ids": need_ids}}


def _need_id(need: Dict[str, Any]) -> str:
    value = need.get("id", need.get("need_id"))
    return value if isinstance(value, str) else ""


def _title_period_conflict(title: str, period: Optional[Dict[str, str]]) -> bool:
    """Use a single title date only to rule out an unsafe quick confirmation.

    A title cannot establish a source's statistical period. Multiple distinct
    dates/months may describe a range or several subjects, so remain unknown.
    """
    if period is None:
        return False
    try:
        start, end = date.fromisoformat(period["start"]), date.fromisoformat(period["end"])
    except (KeyError, TypeError, ValueError):
        return False
    if start > end:
        return False
    days = list(_FULL_DATE.finditer(title))
    if days:
        if len(days) != 1:
            return False
        try:
            subject = date(*(int(part) for part in days[0].groups()))
        except ValueError:
            return False
        return not start <= subject <= end
    without_year_month = _TITLE_YEAR_MONTH.sub(" ", title)
    months = {(int(match.group(1)), int(match.group(2)))
              for match in _TITLE_YEAR_MONTH.finditer(title)}
    months.update((None, int(match.group(1)))
                  for match in _TITLE_MONTH.finditer(without_year_month))
    months = {(year, month) for year, month in months if 1 <= month <= 12}
    if len(months) != 1:
        return False
    year, month = next(iter(months))
    if year is not None:
        first = date(year, month, 1)
        after = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        return end < first or start >= after
    current = start.replace(day=1)
    while current <= end:
        if current.month == month:
            return False
        current = (date(current.year + 1, 1, 1) if current.month == 12
                   else date(current.year, current.month + 1, 1))
    return True


def _receipt_details(need: Dict[str, Any]) -> str:
    receipts = need.get("receipts", [])
    if not isinstance(receipts, list):
        receipts = []
    lines = []
    for receipt in receipts[:3]:
        if not isinstance(receipt, dict):
            continue
        title = str(receipt.get("title", receipt.get("source_id", "本地来源")))[:72]
        period_value = receipt.get("source_period")
        period = ("{} 至 {}".format(period_value.get("start", "?"), period_value.get("end", "?"))
                  if isinstance(period_value, dict) else "统计期未标明")
        captured = str(receipt.get("observed_at") or receipt.get("updated_at")
                       or "采集时间未标明")[:40]
        locator = str(receipt.get("locator", ""))[:96]
        lines.append("{}｜统计期：{}｜采集：{}{}".format(title, period, captured,
            "｜定位：" + locator if locator else ""))
    return "\n".join(lines)


def _source_question(need: Dict[str, Any], period: Optional[Dict[str, str]],
                     attempt: int) -> Dict[str, Any]:
    need_id = _need_id(need)
    status = need["status"]
    title = str(need.get("label", need.get("title", need_id)))[:40]
    target = "目标周期" if period else "本次需求"
    status_text = {"candidate": "找到了候选来源，但与{}的对应关系仍需核对。".format(target),
                   "partial": "找到了部分内容，仍不能证明所需资料完整。",
                   "missing": "本地未找到{}内可核验的完整内容。".format(target),
                   "failed": "本地检索失败，尚不能判断资料是否存在。",
                   "skipped": "本地记忆读取已跳过，尚不能判断资料是否存在。"}[status]
    details = _receipt_details(need)
    options = []
    target_period = dict(period) if period else None
    receipts = [item for item in (need.get("receipts") or []) if isinstance(item, dict)]
    conflicting_titles = [str(item.get("title") or item.get("source_id") or "本地来源")[:72]
                          for item in receipts if item.get("period_status") != "matched"
                          and _title_period_conflict(str(item.get("title") or ""), period)]
    if conflicting_titles:
        details += ("\n" if details else "") + "题名日期与目标周期不符，不能直接确认：{}。".format(
            "、".join(conflicting_titles[:3]))
    if status == "candidate":
        confirmable = [item for item in receipts
                       if item.get("period_status") != "mismatch"
                       and (item.get("period_status") == "matched" or
                            not _title_period_conflict(str(item.get("title") or ""), period))]
        for receipt in confirmable[:3]:
            source_id = receipt.get("source_id")
            if not isinstance(source_id, (str, int)) or source_id == "":
                continue
            option_title = str(receipt.get("title", source_id))[:18]
            options.append({"id": "candidate_" + str(len(options) + 1),
                            "label": "确认「{}」".format(option_title),
                            "description": ("我确认该候选适用于目标周期；仍须保留来源原始日期和完整度。"
                                            if period else "我确认该候选适用于本次需求；仍须保留来源原始信息。"),
                            "details": "来源身份：{}".format(str(source_id)[:100]), "recommended": False,
                            "input_value": {"action": "confirm_candidate", "need_id": need_id,
                                            "target_period": target_period, "source_id": source_id,
                                            "receipt_fingerprint": _stable_id("", {
                                                key: receipt.get(key) for key in ("source_type", "source_id",
                                                    "source_snapshot_id", "source_period", "completeness")})}})
    options.extend([
        {"id": "retry_local", "label": "重新检索本地资料" if status == "failed" else
                     "恢复权限后重试" if status == "skipped" else "继续查找本地资料",
         "description": ("需先恢复记忆访问权限；此处的重试不会自动改变现有权限。"
                         if status == "skipped" else "保持此项待确认，重新核对本地来源与实际时期。"),
         "details": "可用自定义答案补充本地文档名称、原链接或准确定位。",
         "recommended": False,
         "input_value": {"action": "retry_local_recall", "need_id": need_id,
                         "target_period": target_period}},
        {"id": "draft_with_gap", "label": "标明缺口先成稿",
         "description": ("只写已核实内容，将此项尚未核验处标明，不推断资料是否存在。"
                         if status in {"failed", "skipped"} else
                         "只写已核实的部分，将此项缺失或不完整处明确标为待核实。"),
         "details": ("这不会把未核实的历史值写成目标周期事实。" if period else
                     "这不会把未核实的历史值写成当前任务事实。"),
         "recommended": False,
         "input_value": {"action": "draft_with_gap", "need_id": need_id,
                         "target_period": target_period}},
    ])
    return {"id": _stable_id("input_source_", [need_id, period, status, attempt, [
                item.get("source_id") for item in (need.get("receipts") or []) if isinstance(item, dict)]]),
            "parent_question_id": None, "parent_option_id": None,
            "exploration_stage": "explore", "dimension_id": "source_confirmation", "dimension": "资料核对",
            "type": "single_choice", "single_choice_reason": "同一项资料本轮只能采用一种处理方式",
            "prompt": "「{}」的资料如何确认？".format(title),
            "why_now": "资料读取已跳过；{}".format(status_text) if status == "skipped" else
                       "已尝试本地检索；{}".format(status_text),
            "context_details": "{}{}{}".format(
                "目标周期：{} 至 {}。".format(period["start"], period["end"]) if period else "",
                status_text, "\n" + details if details else ""),
            "required": True, "allow_custom": True,
            "answer_template": "可写本地文档名称、原链接、准确定位或说明候选与目标周的关系。",
            "options": options,
            "input_clarification": {"kind": "source_confirmation", "need_ids": [need_id]}}


def _saved_source_handling(decisions: List[Dict[str, Any]],
                           question: Dict[str, Any]) -> str:
    """Resolved needs survive a retry; retry actions apply to one attempt."""
    answer = ""
    valid_values = [option["input_value"] for option in question["options"]]
    need_ids = question["input_clarification"]["need_ids"]
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        marker = decision.get("input_clarification")
        if (not isinstance(marker, dict) or marker.get("kind") != "source_confirmation"
                or marker.get("need_ids") != need_ids
                or decision.get("answer_source", decision.get("source")) != "user"):
            continue
        value = decision.get("input_value")
        if value is None:
            selected = decision.get("selected_options", [])
            if isinstance(selected, list) and len(selected) == 1 and isinstance(selected[0], dict):
                value = selected[0].get("input_value")
        if value in valid_values:
            action = value.get("action")
            if action in {"confirm_candidate", "draft_with_gap"}:
                answer = "confirmed"
            else:
                answer = "retry" if decision.get("question_id") == question["id"] else ""
        elif str(decision.get("custom_text", "")).strip():
            answer = "retry" if decision.get("question_id") == question["id"] else ""
    return answer


def plan_input_clarification_questions(
        *, root_request: str, selected_skills: List[Dict[str, Any]],
        confirmed_decisions: List[Dict[str, Any]], preflight: Optional[Dict[str, Any]],
        as_of_date: Union[str, date], timezone: str = "Asia/Shanghai") -> Dict[str, Any]:
    """Return next input questions and a non-authorizing preflight disposition.

    ``matched`` is supplied by the evidence preflight, not inferred here.
    ``failed`` and ``skipped`` remain distinct from a genuine empty retrieval.
    The caller must persist the user answer and re-run preflight before writing.
    """
    as_of = _day(as_of_date)
    if not isinstance(root_request, str) or not isinstance(timezone, str) or not timezone:
        raise ValueError("invalid clarification context")
    selected_skills = selected_skills or []
    confirmed_decisions = confirmed_decisions or []
    preflight = preflight or {}
    timezone = preflight.get("timezone") or timezone
    if not isinstance(timezone, str) or not timezone:
        raise ValueError("invalid preflight timezone")
    needs = preflight.get("needs", [])
    if not isinstance(needs, list) or any(not isinstance(item, dict) for item in needs):
        raise ValueError("invalid preflight needs")
    ids = [_need_id(item) for item in needs]
    if any(not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("invalid preflight need ids")
    expected = ids if ids else _skill_need_ids(selected_skills)
    period_state = preflight.get("period", {})
    if not isinstance(period_state, dict):
        raise ValueError("invalid preflight period")
    user_answered, saved_period = _saved_user_period(confirmed_decisions, as_of, timezone)
    period = saved_period if user_answered else None
    source = "user_answer" if period else ""
    if not user_answered and period_state.get("status") == "confirmed":
        period = _structured_range(period_state.get("range", period_state), timezone)
        source = "preflight" if period else ""
    if not user_answered and period is None:
        period = _explicit_range(root_request, as_of, timezone, allow_relative=True)
        source = "root_request" if period else ""
    weekly = _weekly_request(root_request, selected_skills)
    candidate_ranges = period_state.get("candidate_ranges", [])
    period_needed = weekly or (period_state.get("status") == "unconfirmed" and
                               isinstance(candidate_ranges, list) and bool(candidate_ranges))
    if period_needed and period is None:
        return {"status": "question", "period": {"status": "unconfirmed"},
                "questions": [_period_question(root_request, as_of, timezone, expected,
                    candidate_ranges=candidate_ranges, cadence=str(period_state.get("cadence") or
                    ("weekly" if weekly else "monthly")))]}
    if not weekly and period is None and not expected:
        return {"status": "not_applicable", "period": {"status": "not_applicable"}, "questions": []}
    period_result = {"status": "confirmed", "range": period, "source": source} if period else {"status": "not_applicable"}
    if not expected:
        return {"status": "ready", "period": period_result, "questions": []}
    if set(ids) != set(expected) or any(item.get("status") not in _NEED_STATUSES for item in needs):
        return {"status": "awaiting_preflight", "period": period_result, "questions": []}
    if any(item["status"] == "pending" for item in needs):
        return {"status": "awaiting_preflight", "period": period_result, "questions": []}
    unresolved = [item for item in needs if item["status"] in {
        "candidate", "partial", "missing", "failed", "skipped"}]
    if unresolved and period is None and period_needed:
        return {"status": "awaiting_period", "period": period_result, "questions": []}
    if unresolved:
        attempt = preflight.get("attempt", 0)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise ValueError("invalid preflight attempt")
        questions = [_source_question(item, period, attempt) for item in unresolved]
        handlings = {item["input_clarification"]["need_ids"][0]: _saved_source_handling(
            confirmed_decisions, item) for item in questions}
        if any(value == "retry" for value in handlings.values()):
            return {"status": "retry_preflight", "period": period_result,
                    "questions": [], "source_handlings": handlings}
        questions = [item for item in questions
                     if handlings[item["input_clarification"]["need_ids"][0]] != "confirmed"]
        if questions:
            return {"status": "question", "period": period_result,
                    "questions": questions, "source_handlings": handlings}
        return {"status": "ready_with_gaps_or_attestation", "period": period_result,
                "questions": [], "source_handlings": handlings}
    return {"status": "ready", "period": period_result, "questions": []}
