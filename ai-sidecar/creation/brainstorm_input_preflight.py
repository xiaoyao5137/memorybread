"""Bounded, source-first input checks for Brainstorm detail questions.

This module owns no database or network access. Callers supply local memory and
data lookup functions, so the same evidence contract works for installed Skills
without teaching question generation about a particular report or dashboard.
Only source receipts, not retrieved bodies or prompts, leave this module.
"""

import copy
import difflib
import hashlib
import inspect
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .brainstorm_content_questions import content_report_source_needs
from .brainstorm_fuzzy_documents import fuzzy_title_score


MAX_NEEDS = 6
MAX_RESULTS = 12
MAX_QUERY_CHARS = 180
MAX_RECEIPTS = 4
_RELATIVE = re.compile(r"(?:本周|这周|这一周|当前周|上周|上一周|最近一周|最新|近期|当前)")
_TOOL_PREFIX = re.compile(r"(?:用|通过|调用)?\s*@?[^\s，。；]{0,18}(?:记忆搜索|数据检索)\s*Tool\s*(?:工具)?")
_DATE = r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"
_DATE_RE = re.compile(_DATE)
_RANGE_RE = re.compile(r"(" + _DATE + r")\s*(?:至|到|~|～|—|–|-)\s*(" + _DATE + r"|\d{1,2}月\d{1,2}日?)")
_ISO_WEEK_RE = re.compile(r"(20\d{2})\s*年\s*第\s*(\d{1,2})\s*周")
_PERIOD_LABEL_RE = re.compile(
    r"(?:统计(?:日期|周期|时间)|数据(?:日期|周期)|报告(?:日期|周期)|会议(?:日期|时间)|发生日期)"
    r"\s*[:：]?\s*(" + _DATE + r")(?:\s*(?:至|到|~|～|—|–|-)\s*(" + _DATE + r"))?"
)
_NUMBERED_TARGET = re.compile(
    r"[①②③④⑤⑥⑦⑧⑨⑩]\s*([^，。；：:]{2,35}?(?:报表|看板|数据集|数据源))"
)
_SENSITIVE_URL_KEY = re.compile(r"(?:token|secret|password|passwd|auth|api.?key|credential|session|sign(?:ature)?)", re.I)


def _text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _local_timezone_label(now: datetime, provided: Optional[str] = None) -> str:
    """Prefer a validated IANA key; an abbreviation is never a zone ID."""
    value = str(provided or "").strip()
    # A previously frozen unambiguous zone must survive a later machine zone
    # change or rerun; only an abbreviation needs local re-resolution.
    if re.fullmatch(r"[+-](?:0\d|1\d|2[0-3]):[0-5]\d", value):
        return value
    if "/" in value and not value.startswith("/"):
        try:
            ZoneInfo(value)
            return value
        except (ZoneInfoNotFoundError, ValueError):
            pass
    candidates = [str(getattr(now.tzinfo, "key", "") or ""),
                  str(os.environ.get("TZ") or "")]
    try:
        localtime = str(Path("/etc/localtime").resolve(strict=True))
        if "/zoneinfo/" in localtime:
            candidates.append(localtime.split("/zoneinfo/", 1)[1])
    except OSError:
        pass
    for candidate in candidates:
        if "/" not in candidate or candidate.startswith("/"):
            continue
        try:
            zone = ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        if now.astimezone(zone).utcoffset() == now.utcoffset():
            return candidate
    offset = now.utcoffset() or timedelta()
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    return "{}{:02d}:{:02d}".format(sign, hours, remainder)


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    matched = _DATE_RE.search(str(value or ""))
    if matched is None:
        return None
    parts = [int(part) for part in re.findall(r"\d+", matched.group(0))[:3]]
    try:
        return date(*parts)
    except (TypeError, ValueError):
        return None


def _confirmed_period(root_request: str, period: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if isinstance(period, dict):
        source = period.get("source")
        if source in {"user", "user_edit", "confirmed_selection"}:
            start, end = _parse_date(period.get("start")), _parse_date(period.get("end"))
            if start and end and start <= end and (end - start).days <= 370:
                return {"start": start.isoformat(), "end": end.isoformat(), "source": str(source)}
    # An explicit range in the root instruction is itself user authority.
    matched = _RANGE_RE.search(root_request)
    if matched:
        start = _parse_date(matched.group(1))
        end = _parse_date(matched.group(2))
        if end is None and start is not None:
            parts = [int(part) for part in re.findall(r"\d+", matched.group(2))]
            try:
                end = date(start.year, *parts[:2])
            except (TypeError, ValueError):
                end = None
        if start and end and start <= end and (end - start).days <= 370:
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "user"}
    week = _ISO_WEEK_RE.search(root_request)
    if week:
        try:
            start = date.fromisocalendar(int(week.group(1)), int(week.group(2)), 1)
            end = date.fromisocalendar(int(week.group(1)), int(week.group(2)), 7)
            return {"start": start.isoformat(), "end": end.isoformat(), "source": "user"}
        except ValueError:
            pass
    return None


def _root_relative_period(root_request: str, current: date) -> Optional[Dict[str, str]]:
    monday = current - timedelta(days=current.weekday())
    if re.search(r"上周|上一周|last week", root_request, re.I):
        return {"start": (monday - timedelta(days=7)).isoformat(),
                "end": (monday - timedelta(days=1)).isoformat(), "source": "user"}
    if re.search(r"本周|这周|这一周|当前周|this week", root_request, re.I):
        return {"start": monday.isoformat(),
                "end": (monday + timedelta(days=6)).isoformat(), "source": "user"}
    return None


def _period_context(root_request: str, selected_skills: List[Dict[str, Any]],
                    confirmed_period: Optional[Dict[str, Any]], current: date) -> Dict[str, Any]:
    skill_text = " ".join(
        str(value)
        for skill in selected_skills if isinstance(skill, dict)
        for value in [skill.get("title"), skill.get("summary")]
    )
    cadence = "weekly" if re.search(r"周报|每周|本周|这周|上周|weekly", root_request + skill_text, re.I) else (
        "monthly" if re.search(r"月报|每月|本月|monthly", root_request + skill_text, re.I) else "")
    confirmed = (_confirmed_period(root_request, confirmed_period)
                 or _root_relative_period(root_request, current))
    if confirmed:
        return {"status": "confirmed", **confirmed, "cadence": cadence,
                "candidate_ranges": []}
    if not cadence:
        return {"status": "not_required", "candidate_ranges": []}
    if cadence == "weekly":
        monday = current - timedelta(days=current.weekday())
        ranges = [
            {"start": (monday - timedelta(days=7)).isoformat(),
             "end": (monday - timedelta(days=1)).isoformat(), "kind": "latest_completed_week"},
            {"start": monday.isoformat(), "end": (monday + timedelta(days=6)).isoformat(),
             "kind": "current_week"},
        ]
    else:
        first = current.replace(day=1)
        prior_end = first - timedelta(days=1)
        next_first = first.replace(year=first.year + 1, month=1) if first.month == 12 else first.replace(month=first.month + 1)
        ranges = [
            {"start": prior_end.replace(day=1).isoformat(), "end": prior_end.isoformat(),
             "kind": "latest_completed_month"},
            {"start": first.isoformat(), "end": (next_first - timedelta(days=1)).isoformat(),
             "kind": "current_month"},
        ]
    return {"status": "unconfirmed", "cadence": cadence, "candidate_ranges": ranges}


def _query_text(label: str, objective: str) -> str:
    # Relative terms choose a period before the user has done so. Exclude them
    # from source discovery, then compare source facts with the confirmed range.
    lead = _RELATIVE.sub("", _TOOL_PREFIX.sub("", label)).strip()
    detail = _RELATIVE.sub("", _TOOL_PREFIX.sub("", objective)).strip()
    detail = re.sub(r"(?:并总结为|并以列表|并形成|并严格按|并添加到|最多\d+行).*", "", detail)
    detail = re.sub(r"^(?:获取|检索|查询|查找|读取|搜索)", "", detail)
    if len(lead) >= 8 and lead in detail:
        value = detail
    else:
        value = " ".join(part for part in (lead, detail) if part)
    return _text(value, MAX_QUERY_CHARS).strip("，。、；:： ")


def _weekly_source_variant(need: Dict[str, Any], period: Dict[str, Any]) -> str:
    """Find a document under a common source title when prose says progress.

    This is a second *retrieval query*, never an inferred fact or source match.
    It uses only words in the selected step and runs at most once per need.
    """
    if need["tool"] != "memory_search" or period.get("cadence") != "weekly":
        return ""
    label = _RELATIVE.sub("", need["label"])
    if "进展" not in label:
        return ""
    focus = re.search(r"关于(.{2,48}?)(?:相关的)?进展", label)
    if focus:
        return _text(focus.group(1).strip("的、， ") + "周进度总结", MAX_QUERY_CHARS)
    subject = re.split(r"(?:的)?进展", label, maxsplit=1)[0].strip("的、， ")
    return _text(subject + "周会", MAX_QUERY_CHARS) if len(subject) >= 4 else ""


def build_input_needs(selected_skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Use executable Skill steps as source targets, capped before retrieval."""
    needs = []
    seen = set()
    for skill in selected_skills:
        if not isinstance(skill, dict):
            continue
        skill_id = _text(skill.get("id") or skill.get("clientSkillKey"), 96)
        steps = skill.get("executionSteps", skill.get("execution_steps", []))
        if not isinstance(steps, list):
            continue
        for position, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            tools = step.get("tools") or []
            if not isinstance(tools, list):
                continue
            tool = ("memory_search" if "memory_search" in tools else
                    "data_search" if "data_search" in tools else "")
            if not tool:
                continue
            step_id = _text(step.get("id") or position, 96)
            title = _text(step.get("title") or step_id, 100)
            objective = _text(step.get("objective"), 800)
            numbered_matches = list(_NUMBERED_TARGET.finditer(objective)) if tool == "data_search" else []
            clean_objective = _TOOL_PREFIX.sub("", objective).strip()
            parent_match = re.search(r"(?:获取|检索|查询|读取)([^，。；：:]{2,50}?)的数据", clean_objective)
            parent_source = _text(parent_match.group(1), 60) if parent_match else ""
            parent_source = _TOOL_PREFIX.sub("", parent_source).strip()
            targets = []
            for index, match in enumerate(numbered_matches):
                end = (numbered_matches[index + 1].start() if index + 1 < len(numbered_matches)
                       else len(objective))
                targets.append((match.group(1).strip(), objective[match.start():end]))
            if not targets:
                independent = []
                if tool == "memory_search" and "以及" in objective:
                    material = re.split(r"并(?:以|总结|形成|添加)|最多\d+行", objective, maxsplit=1)[0]
                    independent = [
                        _text(_TOOL_PREFIX.sub("", part).lstrip("，、 ").removeprefix("获取"), 90)
                        .strip("，。、；:： ")
                        for part in re.split(r"，?以及", material)
                    ]
                    independent = [part for part in independent if len(part) >= 8]
                targets = [(part, part) for part in independent] if len(independent) >= 2 else [(title, objective)]
            for subposition, (label, target_objective) in enumerate(targets):
                period_mode = _text(step.get("sourcePeriodPolicy"), 32)
                if period_mode not in {"full_range", "point_in_period", "event_in_period"}:
                    period_mode = (
                        "point_in_period" if re.search(r"周期内.{0,16}(?:可用)?统计日|最后一个可用统计日", target_objective)
                        else "event_in_period" if re.search(r"周会|会议纪要|会议记录", label + target_objective)
                        else "full_range"
                    )
                query = _query_text(
                    label, target_objective if not numbered_matches else " ".join(
                        part for part in (parent_source, label) if part))
                if not query:
                    continue
                source_form = ("report" if tool == "data_search" else
                               "document" if re.search(r"会议纪要|会议记录|周报|文档", label + objective)
                               else "any")
                identity = (skill_id, step_id, label)
                if identity in seen:
                    continue
                seen.add(identity)
                digest = hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()[:16]
                needs.append({"id": "need-" + digest, "skill_id": skill_id,
                              "skill_step_id": step_id, "label": label, "tool": tool,
                              "query": query, "source_target_index": subposition,
                              "period_mode": period_mode, "source_form": source_form})
                if len(needs) >= MAX_NEEDS:
                    return needs
    return needs


def _mapping(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "__dict__"):
        return vars(item)
    return {}


def _safe_url(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or len(raw) > 1024:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        if any(_SENSITIVE_URL_KEY.search(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))[:768]
    except ValueError:
        return None


def _period_from_text(body: str) -> Optional[Dict[str, str]]:
    # A capture or update date is never a subject-period proof. Only explicit
    # labels in source content can provide a candidate period.
    match = _PERIOD_LABEL_RE.search(body[:12000])
    if not match:
        return None
    start = _parse_date(match.group(1))
    end = _parse_date(match.group(2)) if match.group(2) else start
    if start and end and start <= end:
        return {"start": start.isoformat(), "end": end.isoformat()}
    return None


def _source_period(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    body_period = _period_from_text(str(
        item.get("full_content") or item.get("content_text") or item.get("content_excerpt") or ""))
    if body_period is not None:
        return body_period
    structured = item.get("structured_data")
    if isinstance(structured, dict):
        period = structured.get("period")
        if (isinstance(period, dict)
                and period.get("origin") in {"source_page", "metric_rows", "source_document"}):
            start = _parse_date(period.get("start") or period.get("period_start"))
            end = _parse_date(period.get("end") or period.get("period_end"))
            if start and end and start <= end:
                return {"start": start.isoformat(), "end": end.isoformat()}
    if item.get("period_origin") in {"source_page", "metric_rows", "source_document"}:
        start = _parse_date(item.get("period_start"))
        end = _parse_date(item.get("period_end"))
        if start and end and start <= end:
            return {"start": start.isoformat(), "end": end.isoformat()}
    return None


def _period_status(source_period: Optional[Dict[str, str]], period: Dict[str, Any], mode: str) -> str:
    if period["status"] != "confirmed" or source_period is None:
        return "unknown"
    if source_period["end"] < period["start"] or source_period["start"] > period["end"]:
        return "mismatch"
    if mode in {"point_in_period", "event_in_period"}:
        return ("matched" if (source_period["start"] >= period["start"]
                              and source_period["end"] <= period["end"])
                else "mismatch")
    return "matched" if (source_period["start"] <= period["start"]
                         and source_period["end"] >= period["end"]) else "mismatch"


def _completeness(item: Dict[str, Any]) -> str:
    structured = item.get("structured_data")
    if isinstance(structured, dict):
        collection = structured.get("completeness")
        pagination = structured.get("pagination")
        if isinstance(collection, dict):
            if (collection.get("truncated") is True
                    or str(collection.get("status") or "").lower() in {"partial", "truncated", "incomplete"}):
                return "partial"
        if isinstance(pagination, dict) and pagination.get("dataset_complete") is False:
            return "partial"
    values = [str(item.get(key) or "").lower() for key in (
        "refresh_completeness", "source_completeness", "review_status", "status")]
    if item.get("refresh_truncated") or any(value in {"partial", "truncated", "incomplete", "raw_capture"} for value in values):
        return "partial"
    if (isinstance(structured, dict) and isinstance(structured.get("completeness"), dict)
            and structured["completeness"].get("status") == "complete"):
        return "complete"
    if any(value in {"complete", "completed", "verified"} for value in values):
        return "complete"
    return "unknown"


def _receipt(item: Any, origin: str, period: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    value = _mapping(item)
    source_id = value.get("source_id") or value.get("id")
    source_type = _text(value.get("source_type") or value.get("source_kind") or origin, 32)
    if source_id is None:
        return None
    subject_period = _source_period(value)
    period_status = _period_status(subject_period, period, mode)
    url = _safe_url(value.get("source_url"))
    return {
        "source_type": source_type, "source_id": source_id,
        "source_snapshot_id": value.get("source_snapshot_id"),
        "title": _text(value.get("title") or "未命名来源", 120),
        "source_url": url, "url_redacted": bool(value.get("source_url") and not url),
        "locator": (url or "{}:{}".format(source_type, source_id))[:768],
        "updated_at": value.get("updated_at"),
        "observed_at": value.get("observed_at") or value.get("collected_at"),
        "source_period": subject_period,
        "period_status": period_status,
        "completeness": _completeness(value),
        "can_use": value.get("can_use") is not False,
    }


def _receipt_affinity(receipt: Dict[str, Any], need: Dict[str, Any]) -> float:
    def normalized(text: Any) -> str:
        return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", str(text or "").lower())

    target = normalized(need["label"])
    query = normalized(need["query"])
    title = normalized(receipt["title"])
    similarity = max(
        difflib.SequenceMatcher(None, candidate, title).ratio()
        for candidate in (target, query, normalized(receipt.get("_lookup_query")))
        if candidate and title
    ) if title and (target or query) else 0.0
    similarity = max(similarity, fuzzy_title_score(need["label"], receipt["title"]),
                     fuzzy_title_score(need["query"], receipt["title"]))
    source_type = receipt["source_type"]
    if need["tool"] == "data_search":
        source_bonus = 1.0 if source_type in {"report_url", "data"} else 0.0
    else:
        source_bonus = 1.0 if source_type in {"document", "pending_document"} else 0.0
    return source_bonus + similarity


def _need_status(receipts: List[Dict[str, Any]], failures: List[str],
                 period: Dict[str, Any], need: Dict[str, Any]) -> str:
    if not receipts:
        return "failed" if failures else "missing"
    def form_matches(item: Dict[str, Any]) -> bool:
        title_terms = need.get("source_title_terms")
        if isinstance(title_terms, list) and title_terms and not any(
                isinstance(term, str) and term and term.lower() in item["title"].lower()
                for term in title_terms):
            return False
        if need.get("source_form") == "document":
            return item["source_type"] == "document"
        if need.get("source_form") == "report":
            return item["source_type"] in {"report_url", "data"}
        return True

    if period["status"] in {"confirmed", "not_required"} and any(
            (period["status"] == "not_required" or item["period_status"] == "matched")
            and item["completeness"] == "complete" and item["can_use"]
            and form_matches(item)
            for item in receipts):
        return "matched"
    if any(item["completeness"] == "partial" and item["can_use"]
           and item["period_status"] != "mismatch" for item in receipts):
        return "partial"
    return "candidate"


async def _call(callback: Callable[..., Any], *args: Any) -> Any:
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result


async def run_input_preflight(
    root_request: str,
    selected_skills: List[Dict[str, Any]],
    *,
    memory_allowed: bool,
    memory_lookup: Callable[[str, int], Any],
    data_lookup: Optional[Callable[[str, Dict[str, Any], int], Any]] = None,
    confirmed_period: Optional[Dict[str, Any]] = None,
    today: Optional[date] = None,
    timezone: Optional[str] = None,
    supplemental_queries: Optional[Dict[str, str]] = None,
    content_mode: bool = False,
    brief_edits: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Check source availability before creating detail choices.

    `memory_lookup` is called first for every target, even when it uses a data
    tool during writing. A data lookup only discovers a report source; it never
    proves this period's values without an explicit subject period and complete
    content receipt. Neither lookup failure nor an old candidate asks the user
    for a link until the UI has shown the precise unresolved input.
    """
    current = today or datetime.now().astimezone().date()
    local_timezone = _local_timezone_label(datetime.now().astimezone(), timezone)
    period = _period_context(root_request, selected_skills, confirmed_period, current)
    needs = build_input_needs(selected_skills)
    if content_mode and not selected_skills and len(needs) < MAX_NEEDS:
        report_needs = content_report_source_needs(root_request, brief_edits or {})
        needs.extend(report_needs[:MAX_NEEDS - len(needs)])
    if not memory_allowed:
        skipped = []
        for need in needs:
            public = {key: value for key, value in need.items() if key != "query"}
            public.update(status="skipped", receipts=[], lookup_status="skipped")
            skipped.append(public)
        return {"schema_version": 1, "status": "skipped",
                "as_of_local_date": current.isoformat(), "timezone": local_timezone,
                "period": period,
                "needs": skipped}
    results = []
    for need in needs:
        receipts = []
        failures = []
        queries = [need["query"]]
        supplemental = (supplemental_queries or {}).get(need["id"])
        if isinstance(supplemental, str):
            locator = _text(supplemental, MAX_QUERY_CHARS)
            if locator and locator != need["query"]:
                queries.append(locator)
        if len(queries) == 1:
            variant = _weekly_source_variant(need, period)
            if variant and variant != need["query"]:
                queries.append(variant)
        for query_index, query in enumerate(queries):
            try:
                found = await _call(memory_lookup, query, MAX_RESULTS)
                for item in list(found or [])[:MAX_RESULTS]:
                    receipt = _receipt(item, "memory", period, need["period_mode"])
                    if receipt is not None:
                        receipt["_lookup_bucket"] = query_index
                        receipt["_lookup_query"] = query
                        receipts.append(receipt)
            except Exception:
                failures.append("memory_search")
        if need["tool"] == "data_search":
            if data_lookup is None:
                failures.append("data_search")
            else:
                for data_index, data_query in enumerate(queries):
                    try:
                        found = await _call(data_lookup, data_query, period, MAX_RESULTS)
                        for item in list(found or [])[:MAX_RESULTS]:
                            receipt = _receipt(item, "data", period, need["period_mode"])
                            if receipt is not None:
                                receipt["_lookup_bucket"] = len(queries) + data_index
                                receipt["_lookup_query"] = data_query
                                receipts.append(receipt)
                    except Exception:
                        failures.append("data_search")
        unique = []
        seen = set()
        receipts.sort(key=lambda receipt: _receipt_affinity(receipt, need), reverse=True)
        # Reserve one candidate from each independent lookup, so the fallback
        # that found an actual source is not crowded out by four generic hits
        # from the first query. Then fill remaining slots by affinity.
        buckets = list(dict.fromkeys(receipt["_lookup_bucket"] for receipt in receipts))
        ordered = []
        for bucket in buckets:
            first = next((receipt for receipt in receipts
                          if receipt["_lookup_bucket"] == bucket), None)
            if first is not None:
                ordered.append(first)
        ordered.extend(receipts)
        for receipt in ordered:
            identity = (receipt["source_type"], str(receipt["source_id"]),
                        str(receipt["source_snapshot_id"]))
            if identity in seen:
                continue
            seen.add(identity)
            unique.append({key: value for key, value in receipt.items()
                           if not key.startswith("_lookup_")})
            if len(unique) == MAX_RECEIPTS:
                break
        status = _need_status(unique, failures, period, need)
        public = {key: value for key, value in need.items() if key != "query"}
        public.update(status=status, receipts=unique,
                      lookup_status="partial" if failures and unique else
                      "failed" if failures else "completed")
        results.append(public)
    if not results:
        aggregate = "empty"
    elif all(item["status"] == "matched" for item in results):
        aggregate = "matched"
    elif all(item["status"] == "failed" for item in results):
        aggregate = "failed"
    elif all(item["status"] == "missing" for item in results):
        aggregate = "empty"
    else:
        aggregate = "partial"
    return {"schema_version": 1, "status": aggregate,
            "as_of_local_date": current.isoformat(), "timezone": local_timezone,
            "period": period, "needs": results}


def reclassify_input_preflight(preflight: Dict[str, Any], confirmed_period: Dict[str, Any]) -> Dict[str, Any]:
    """Recheck saved receipts after a user's period choice, with no new reads."""
    if preflight.get("schema_version") != 1:
        raise ValueError("unsupported input preflight version")
    resolved = _confirmed_period("", confirmed_period)
    if resolved is None:
        raise ValueError("period must be a valid user-confirmed range")
    output = copy.deepcopy(preflight)
    output["period"] = {"status": "confirmed", **resolved, "candidate_ranges": []}
    for need in output.get("needs", []):
        if need.get("status") == "skipped":
            continue
        receipts = need.get("receipts") or []
        for receipt in receipts:
            receipt["period_status"] = _period_status(
                receipt.get("source_period"), output["period"], need.get("period_mode", "full_range"))
        failures = ["lookup"] if need.get("lookup_status") in {"failed", "partial"} else []
        need["status"] = _need_status(receipts, failures, output["period"], need)
    statuses = [need.get("status") for need in output.get("needs", [])]
    output["status"] = (
        "empty" if not statuses or all(status == "missing" for status in statuses) else
        "matched" if all(status == "matched" for status in statuses) else
        "failed" if all(status == "failed" for status in statuses) else
        "skipped" if all(status == "skipped" for status in statuses) else "partial"
    )
    return output
