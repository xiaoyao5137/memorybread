"""Validate a user's source-gap choice against the finished document.

Only a saved answer to the exact preflight question can authorize a provisional
section. The choice never changes the statistical period of a retrieved source.
"""

import re
from datetime import date
from typing import Any, Dict, List, Tuple

from .brainstorm_input_preflight import build_input_needs


_UNCERTAIN = re.compile(r"待核实|未核实|尚未核实|缺口|未取得|未找到|无法核验|暂无可核验")
_CURRENT_CLAIM = re.compile(r"本周|当周|本期|当期|目标周期|当前周期")
_REFERENCE_CLAIM = re.compile(r"历史|参考|非本周|而非|不是|不作为|不代表|不等于")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", re.M)
_TABLE_RULE = re.compile(r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$")


def authorized_gap_steps(brief: Any, selected_skills: Any) -> Dict[Tuple[str, str], List[Dict[str, str]]]:
    """Return steps whose every source need has an exact user gap decision."""
    if not isinstance(brief, dict) or not isinstance(selected_skills, list):
        return {}
    period = brief.get("confirmed_report_period")
    if not isinstance(period, dict) or period.get("source") not in {"user", "user_edit", "confirmed_selection"}:
        return {}
    expected = {key: period.get(key) for key in ("start", "end", "timezone")}
    if not all(isinstance(value, str) and value for value in expected.values()):
        return {}
    choices = set()
    for item in brief.get("history") or []:
        if not isinstance(item, dict):
            continue
        question, answer = item.get("question"), item.get("answer")
        if not isinstance(question, dict) or not isinstance(answer, dict) or answer.get("source") != "user":
            continue
        marker = question.get("input_clarification") or {}
        if marker.get("kind") != "source_confirmation" or len(marker.get("need_ids") or []) != 1:
            continue
        selected_ids = answer.get("selected_option_ids")
        if not isinstance(selected_ids, list) or len(selected_ids) != 1:
            continue
        option = next((option for option in question.get("options") or []
                       if isinstance(option, dict) and option.get("id") == selected_ids[0]), None)
        value = option.get("input_value") if isinstance(option, dict) else None
        if (isinstance(value, dict) and value.get("action") == "draft_with_gap"
                and value.get("need_id") == marker["need_ids"][0]
                and value.get("target_period") == expected):
            choices.add(value["need_id"])
    grouped: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
    for need in build_input_needs(selected_skills):
        key = (need["skill_id"], need["skill_step_id"])
        grouped.setdefault(key, []).append({"id": need["id"], "label": need["label"]})
    return {key: needs for key, needs in grouped.items()
            if needs and all(need["id"] in choices for need in needs)}


def gap_disclosure_corrections(document: str, brief: Any,
                               receipts: List[Dict[str, Any]]) -> List[str]:
    """Require a section-level disclosure before accepting a provisional draft."""
    period = brief.get("confirmed_report_period") if isinstance(brief, dict) else None
    if not isinstance(period, dict):
        return []
    start, end = str(period.get("start") or ""), str(period.get("end") or "")
    sections = []
    headings = list(_HEADING.finditer(document))
    for index, heading in enumerate(headings):
        level = len(heading.group(0).lstrip().split(" ", 1)[0])
        end_at = len(document)
        for later in headings[index + 1:]:
            later_level = len(later.group(0).lstrip().split(" ", 1)[0])
            if later_level <= level:
                end_at = later.start()
                break
        sections.append((heading.group(1).strip(), document[heading.end():end_at]))
    corrections = []
    if any(item.get("acknowledged_gaps") for item in receipts) and (start not in document or end not in document):
        corrections.append("在正文显著位置写明用户确认的报告周期 {} 至 {}。".format(start, end))
    for receipt in receipts:
        if not receipt.get("acknowledged_gaps"):
            continue
        title = str(receipt.get("title") or "").strip()
        section = next((body for heading, body in sections
                        if title and (title in heading or heading in title)), None)
        if section is None:
            corrections.append("补齐「{}」章节并说明该目标周期来源缺口。".format(title))
            continue
        if not _UNCERTAIN.search(section[:1000]):
            corrections.append("在「{}」章节开头明确写出目标周期资料或指标待核实，旧来源不得当作本周事实。".format(title))
        for paragraph in re.split(r"\n\s*\n|(?<=[。！？])", section):
            clean = paragraph.strip()
            if not clean or clean.startswith(("|", "#")):
                continue
            if (_CURRENT_CLAIM.search(clean) and not _UNCERTAIN.search(clean)
                    and not _REFERENCE_CLAIM.search(clean)):
                corrections.append("「{}」中「本周/本期」的事实断言无目标周期来源，请撤回或写明实际来源期并标为历史参考。".format(title))
                break
    return list(dict.fromkeys(corrections))


def _table_shells(body: str) -> List[str]:
    lines = body.splitlines()
    tables = []
    for index in range(len(lines) - 1):
        header, rule = lines[index].strip(), lines[index + 1].strip()
        if not (header.startswith("|") and header.endswith("|") and _TABLE_RULE.fullmatch(rule)):
            continue
        cells = [cell.strip() for cell in header.strip("|").split("|")]
        if len(cells) < 2 or len(cells) > 24:
            continue
        tables.append("{}\n{}\n| {} |".format(
            header, rule, " | ".join(["待核实"] + ["—"] * (len(cells) - 1))))
        if len(tables) >= 4:
            break
    return tables


def revise_acknowledged_gap_preview(
        document: str, brief: Any, receipts: List[Dict[str, Any]],
        *, protected_base: str = "", force: bool = False) -> Tuple[str, List[Dict[str, Any]]]:
    """Retract unverified step bodies while keeping their ordered table schema.

    This is a bounded fallback for an already generated preview. It does not
    invent source facts, touch protected earlier chapters, or alter the user's
    Skill selection. The original preview is archived by the caller.
    """
    if protected_base and not document.startswith(protected_base):
        return document, []
    period = brief.get("confirmed_report_period") if isinstance(brief, dict) else None
    if (not isinstance(period, dict)
            or period.get("source") not in {"user", "user_edit", "confirmed_selection"}):
        return document, []
    try:
        start = date.fromisoformat(str(period.get("start") or ""))
        end = date.fromisoformat(str(period.get("end") or ""))
    except ValueError:
        return document, []
    if start > end:
        return document, []
    selected = [item for item in receipts if item.get("acknowledged_gaps")]
    if not selected:
        return document, []
    headings = list(_HEADING.finditer(document))
    replacements = []
    edits = []
    matched_starts = []
    period_label = "{} 至 {}".format(start.isoformat(), end.isoformat())
    for receipt in selected:
        title = str(receipt.get("title") or "").strip()
        matched = next((match for match in headings
                        if title and (title in match.group(1) or match.group(1) in title)
                        and match.start() >= len(protected_base)), None)
        if matched is None:
            continue
        matched_starts.append(matched.start())
        level = len(matched.group(0).lstrip().split(" ", 1)[0])
        end_at = len(document)
        for later in headings:
            if later.start() <= matched.start():
                continue
            later_level = len(later.group(0).lstrip().split(" ", 1)[0])
            if later_level <= level:
                end_at = later.start()
                break
        old_body = document[matched.end():end_at]
        needs = [str(value).strip() for value in receipt.get("gap_need_labels") or []
                 if str(value).strip()]
        need_text = "、".join(dict.fromkeys(needs)) or title
        paragraph = (
            "**报告周期：{}；本节状态：待核实。** 本地已检索「{}」，"
            "尚未取得能够确认覆盖目标周期的完整资料或指标。"
            "历史资料和统计期不明的候选仅作参考，不作为本周事实。\n\n"
            "- 本周可确认结论：待核实。\n"
            "- 待核实资料：目标周期内的原始内容或报表截图、可访问定位、实际统计期及完整数值。"
        ).format(period_label, need_text)
        tables = _table_shells(old_body)
        if tables:
            paragraph += "\n\n**目标周期数据表（保留原列，数值待核实）**\n\n" + "\n\n".join(tables)
        replacement = "\n\n" + paragraph + "\n\n"
        replacements.append((matched.end(), end_at, replacement))
        edits.append({"kind": "acknowledged_source_gap", "skill_id": receipt.get("skill_id"),
                      "step_id": receipt.get("step_id"), "retained_table_schemas": len(tables),
                      "removed_chars": len(old_body)})
    if matched_starts:
        # The generated introduction belongs to this chapter, not to the
        # protected earlier document. A polisher may put unverified "this week"
        # claims there before the first Skill heading, outside every section
        # replacement above. Retain only the document/chapter wrapper headings.
        step_starts = [heading.start() for heading in headings
                       if heading.start() >= len(protected_base)
                       and any(str(receipt.get("title") or "").strip()
                               and (str(receipt.get("title") or "").strip() in heading.group(1)
                                    or heading.group(1) in str(receipt.get("title") or "").strip())
                               for receipt in receipts)]
        first_step = min(step_starts) if step_starts else min(matched_starts)
        intro = document[len(protected_base):first_step]
        chapter = brief.get("chapter_context") if isinstance(brief, dict) else None
        chapter_title = str(chapter.get("title") or "").strip() if isinstance(chapter, dict) else ""
        wrappers = []
        unexpected_heading = False
        seen_title = False
        for heading in _HEADING.finditer(intro):
            level = len(heading.group(0).lstrip().split(" ", 1)[0])
            title = heading.group(1).strip()
            if level == 1 and not seen_title:
                wrappers.append(heading.group(0).strip())
                seen_title = True
            elif level == 2 and chapter_title and title == chapter_title:
                wrappers.append(heading.group(0).strip())
            else:
                unexpected_heading = True
        intro_body = _HEADING.sub("", intro).strip()
        if intro_body or unexpected_heading:
            safe_intro = (
                "**报告周期：{}；总览：待核实。** 目标周期资料不完整，"
                "以下章节按已确认的缺口标注；历史来源不作为本周事实。"
            ).format(period_label)
            prefix = "\n\n" if protected_base else ""
            replacement = prefix + "\n\n".join(wrappers + [safe_intro]) + "\n\n"
            replacements.append((len(protected_base), first_step, replacement))
            edits.append({"kind": "acknowledged_source_gap_intro",
                          "removed_chars": len(intro)})
    if not replacements:
        return document, []
    revised = document
    for start_at, end_at, replacement in sorted(replacements, reverse=True):
        revised = revised[:start_at] + replacement + revised[end_at:]
    if (start.isoformat() not in revised or end.isoformat() not in revised) and headings:
        first = headings[0]
        if first.start() >= len(protected_base):
            revised = revised[:first.end()] + "\n\n报告周期：{}。".format(period_label) + revised[first.end():]
            edits.append({"kind": "confirmed_report_period", "period": period_label})
    return revised, edits


def retract_content_gap_preview(
        document: str, brief: Any, selected_skills: Any,
        *, protected_base: str = "") -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    """Replace only a v17 no-Skill all-gap chapter with a provisional body.

    The authorization is reconstructed from the current saved preflight and
    answers. A source-gap choice authorizes a draft with visible gaps, never a
    factual claim. The caller keeps the original candidate as audit evidence.
    """
    untouched = (document, [], {"status": "not_applicable"})
    if not isinstance(document, str) or not isinstance(protected_base, str):
        return document, [], {"status": "invalid", "reason": "invalid_document"}
    if not isinstance(brief, dict) or not isinstance(selected_skills, list):
        return untouched
    contract = brief.get("content_source_gap")
    if isinstance(contract, dict):
        if selected_skills or contract.get("schema_version") != "brainstorm.content-source-gap.v1":
            return untouched
        if contract.get("preparation_contract_version") != 17:
            return untouched
        period = contract.get("confirmed_period")
        preflight = contract.get("input_preflight")
    else:
        # Local preparation tests can pass the full saved workflow. During a
        # real chapter Core sends only the bounded private contract above.
        workflow = brief.get("stage_workflow")
        if not isinstance(workflow, dict) or workflow.get("version") != 2:
            return untouched
        stages = workflow.get("stages")
        if not isinstance(stages, list):
            return untouched
        by_id = {stage.get("id"): stage for stage in stages if isinstance(stage, dict)}
        if len(stages) != 3 or len(by_id) != 3 or set(by_id) != {"direction", "detail", "writing"}:
            return untouched
        if selected_skills or any(
                stage.get("preparation_contract_version") != 17 for stage in by_id.values()):
            return untouched
        detail = by_id["detail"]
        period = detail.get("confirmed_period")
        preflight = detail.get("input_preflight")

    def invalid(reason: str) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        return document, [], {"status": "invalid", "reason": reason}

    if not document.startswith(protected_base):
        return invalid("protected_base_mismatch")
    if not isinstance(preflight, dict):
        return invalid("missing_preflight")
    preflight_period = preflight.get("period")
    if not isinstance(period, dict) or not isinstance(preflight_period, dict):
        return invalid("missing_confirmed_period")
    if period.get("source") not in {"user", "user_edit", "confirmed_selection"}:
        return invalid("period_not_user_confirmed")
    expected = {key: period.get(key) for key in ("start", "end", "timezone")}
    if not all(isinstance(value, str) and value for value in expected.values()):
        return invalid("invalid_confirmed_period")
    try:
        if date.fromisoformat(expected["start"]) > date.fromisoformat(expected["end"]):
            return invalid("invalid_confirmed_period")
    except ValueError:
        return invalid("invalid_confirmed_period")
    if (preflight_period.get("status") != "confirmed"
            or any(preflight_period.get(key) != expected[key] for key in ("start", "end"))
            or preflight_period.get("source") != period.get("source")
            or preflight.get("timezone") != expected["timezone"]):
        return invalid("preflight_period_mismatch")
    brief_period = brief.get("confirmed_report_period")
    if brief_period is not None and (
            not isinstance(brief_period, dict)
            or any(brief_period.get(key) != value for key, value in expected.items())
            or brief_period.get("source") != period.get("source")):
        return invalid("brief_period_mismatch")

    needs = preflight.get("needs")
    if not isinstance(needs, list) or not needs:
        return invalid("missing_source_needs")
    need_ids = []
    labels = []
    for need in needs:
        if not isinstance(need, dict):
            return invalid("invalid_source_need")
        need_id, label = need.get("id"), need.get("label")
        if (not isinstance(need_id, str) or not need_id
                or need_id in need_ids or not isinstance(label, str)
                or not label.strip() or len(label) > 160):
            return invalid("invalid_source_need")
        need_ids.append(need_id)
        labels.append(re.sub(r"\s+", " ", label).strip())

    history = brief.get("history")
    if not isinstance(history, list):
        return invalid("missing_source_answers")
    answered = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        question, answer = item.get("question"), item.get("answer")
        if not isinstance(question, dict):
            continue
        marker = question.get("input_clarification")
        if not isinstance(marker, dict) or marker.get("kind") != "source_confirmation":
            continue
        marker_ids = marker.get("need_ids")
        if (not isinstance(marker_ids, list) or len(marker_ids) != 1
                or not isinstance(marker_ids[0], str)):
            return invalid("source_answer_need_mismatch")
        need_id = marker_ids[0]
        if need_id not in need_ids:
            # A saved retry or an earlier period may have a different need.
            # It remains in history for audit but is not the current receipt.
            continue
        if not isinstance(answer, dict) or answer.get("source") != "user":
            answered[need_id] = "source_answer_not_user"
            continue
        selected_ids = answer.get("selected_option_ids")
        if not isinstance(selected_ids, list) or len(selected_ids) != 1:
            answered[need_id] = "source_answer_not_gap"
            continue
        options = question.get("options")
        if not isinstance(options, list):
            answered[need_id] = "source_answer_not_gap"
            continue
        selected = [option for option in options if isinstance(option, dict)
                    and option.get("id") == selected_ids[0]]
        if len(selected) != 1:
            answered[need_id] = "source_answer_not_gap"
            continue
        value = selected[0].get("input_value")
        if (not isinstance(value, dict) or value.get("action") != "draft_with_gap"
                or value.get("need_id") != need_id
                or value.get("target_period") != expected):
            answered[need_id] = "source_answer_not_gap"
            continue
        answered[need_id] = "confirmed_gap"
    if set(answered) != set(need_ids):
        return invalid("missing_source_answers")
    for need_id in need_ids:
        if answered[need_id] != "confirmed_gap":
            return invalid(answered[need_id])

    period_label = "{} 至 {}".format(expected["start"], expected["end"])
    chapter = brief.get("chapter_context")
    chapter_title = str(chapter.get("title") or "").strip() if isinstance(chapter, dict) else ""
    chapter_title = re.sub(r"\s+", " ", chapter_title)[:120]
    if protected_base:
        body = "\n\n## {}\n\n".format(chapter_title or "目标周期资料缺口（待核实）")
    else:
        body = "# 报告草稿（待核实）\n\n"
        if isinstance(chapter, dict) and chapter.get("total", 1) > 1:
            body += "## {}\n\n".format(chapter_title or "目标周期资料缺口（待核实）")
    body += ("**报告周期：{}；当前状态：待核实。** 已选择先标明资料缺口成稿。"
             "本地候选资料的存在、采集日期或标题均不能证明其覆盖本期，"
             "以下项目尚无可用于本期事实结论的完整来源。\n\n").format(period_label)
    body += ("当前可确认：报告周期和逐项标明资料缺口的处理方式。"
             "取得原始资料后，先核对实际统计期、业务范围、计量单位和完整数值；"
             "可比口径一致时才计算变化，缺项继续标为待核实。"
             "本期费用、用量、措施及节省均未形成可核验结论。\n\n")
    body += "待核实资料与数据：\n\n"
    body += "\n".join("- {}：需核对原始来源、实际统计期、覆盖范围和完整数值。".format(
        label) for label in labels)
    body += ("\n\n本期数据、变化及结果：待核实。"
             "历史资料只作检索线索，不作为本期已确认结果。\n")
    revised = protected_base + body
    audit = {"status": "converted", "confirmed_period": expected,
             "need_ids": need_ids, "protected_base_preserved": True,
             "already_safe": revised == document}
    if revised == document:
        return document, [], audit
    edits = [{"kind": "v17_content_gap_retraction", "need_ids": need_ids,
              "removed_chars": len(document) - len(protected_base),
              "protected_base_chars": len(protected_base)}]
    return revised, edits, audit
