"""Narrow a normal creation Agent Loop to one confirmed chapter at a time."""

import json
import re
from typing import Any, Dict, List, Optional

from .brief_context import effective_brief_decisions
from .operations import OperationError


def chapter_context(brief: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(brief, dict) or "chapter_context" not in brief:
        return None
    value = brief["chapter_context"]
    if not isinstance(value, dict):
        raise OperationError("CREATION_DOCUMENT_INVALID", "章节创作上下文无效，请恢复当前章节后重试")
    strings = ("id", "title", "goal")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in strings):
        raise OperationError("CREATION_DOCUMENT_INVALID", "章节创作缺少有效的标题或目标")
    index, total, outline = value.get("index"), value.get("total"), value.get("outline")
    if (type(index) is not int or type(total) is not int or not 1 <= index <= total
            or not isinstance(outline, list) or len(outline) != total
            or any(not isinstance(item, dict) or any(not isinstance(item.get(key), str)
                   or not item[key].strip() for key in strings) for item in outline)
            or any(outline[index - 1][key] != value[key] for key in strings)
            or len({item["id"] for item in outline}) != len(outline)
            or len({chapter_title_key(item["title"]) for item in outline}) != len(outline)
            or not isinstance(value.get("confirmed_decisions"), list)):
        raise OperationError("CREATION_DOCUMENT_INVALID", "章节顺序或已确认设计无效，请恢复当前章节后重试")
    return value


def chapter_goal(chapter: Dict[str, Any]) -> str:
    if chapter["total"] == 1:
        return ("本轮完成唯一成文单元的完整正文：{}。按当前有效创作需求和所选技能规定的正文结构展开；"
                "保留此前全部正文、内部分节、固定内容与方法，并独立完成本轮交付核验。".format(chapter["goal"]))
    return ("本轮仅追加第 {}/{} 章《{}》：{}。保留此前全部正文，当前章独立完成交付核验；"
            "其他章节尚未轮到创作，不纳入本轮必写内容，也不得提前生成。".format(
                chapter["index"], chapter["total"], chapter["title"], chapter["goal"]))


def chapter_routing_context(brief: Any, has_document: bool) -> Optional[Dict[str, Any]]:
    """Describe a validated workflow without manufacturing its routing result."""
    chapter = chapter_context(brief)
    if chapter is None:
        return None
    return {"kind": "confirmed_chapter", "id": chapter["id"],
            "title": chapter["title"], "goal": chapter["goal"],
            "index": chapter["index"], "total": chapter["total"],
            "document_scope": "append_existing_document" if has_document else "first_chapter_of_new_document",
            "workflow_steps_are_separate_deliverables": False}


def chapter_delivery_scope(brief: Any, base: str) -> Optional[Dict[str, Any]]:
    """The durable input body, never an outline or candidate, defines history."""
    chapter = chapter_context(brief)
    if chapter is None:
        return None
    return {"current_chapter": {key: chapter[key] for key in ("id", "title", "goal", "index", "total")},
            "protected_previous_body_is_empty": not base.strip(),
            "protected_previous_body_characters": len(base),
            "protected_previous_body_source": "provided_materials.original_document",
            "preservation_rule": ("此前已完成正文为空，无历史章节需要补回；交付当前完整正文。" if not base.strip() and chapter["total"] == 1
                else "此前已完成正文为空，无历史章节需要补回；只能交付本章。" if not base.strip()
                else "仅 original_document 是必须逐字保留的此前已完成正文，不能按目录补造历史内容。"),
            "unwritten_chapters": [{"id": item["id"], "title": item["title"]}
                                   for item in chapter["outline"][chapter["index"]:]],
            "scope_rule": ("唯一成文单元包含当前需求和所选技能规定的完整正文与内部分节；内部标题不是未确认的后续写作单元。"
                "目录与候选不是历史正文，只有 original_document 中实际存在的正文需要逐字保留。" if chapter["total"] == 1 else
                "目录与候选不是历史正文。后续章节尚未创作，不要求覆盖，也不得出现标题、占位或正文。")}


def chapter_decisions(brief: Dict[str, Any]) -> List[Dict[str, str]]:
    chapter = chapter_context(brief)
    all_decisions = effective_brief_decisions(brief)
    if chapter is None or chapter["total"] == 1:
        return all_decisions
    identifiers = {str(item.get("question_id") or "") for item in chapter["confirmed_decisions"]
                   if isinstance(item, dict) and item.get("source", item.get("answer_source")) == "user"}
    # Rebind against the current authoritative brief, including edits and clears;
    # the embedded chapter list cannot resurrect an obsolete or unchosen answer.
    return [item for item in all_decisions if item.get("question_id") in identifiers]


def chapter_prompt_context(brief: Dict[str, Any]) -> str:
    chapter = chapter_context(brief)
    if chapter is None:
        return ""
    current = chapter_decisions(brief)
    current_ids = {item["id"] for item in current}
    background = [item for item in effective_brief_decisions(brief) if item["id"] not in current_ids]
    edits = brief.get("brief_edits") if isinstance(brief.get("brief_edits"), dict) else {}
    root = str(edits.get("root_request", brief.get("root_request")) or "")
    from .brainstorm import BrainstormCoordinator
    memory_allowed = BrainstormCoordinator._memory_allowed(root, brief.get("decisions") or [], edits,
                                                           brief.get("user_input_revisions"))
    single = chapter["total"] == 1
    title_scope = ("只输出交给读者的完整正文。正文标题及内部标题层级依据当前有效创作需求和所选技能，"
        "不把写作单元名称强制用作正文标题，不额外套一章标题；保留实际要求的标题与内部分节。" if single else
        "只输出交给读者的章节正文。章节标题文本为：{}。标题不加书名号，不重复同名标题；"
        "若既有文档身份标题与本章同名且当前是首章，该一级标题即可承担本章标题，不再复制二级标题。"
        "其他情况用二级标题标记新增章节。".format(chapter["title"]))
    return "\n\n".join([
        chapter_goal(chapter),
        title_scope + "以 chapter_delivery_scope 指向的原始正文为已完成内容，逐字保留并在末尾追加；"
        + ("当前候选可修改本轮正文。" if single else "当前候选可修改本章。") + "若返回完整文档，完整携带原正文。"
        "计划、检查、交付属于系统执行过程，不写入正文；不添加已遵循简报、自检通过、未编造信息等完成报告或自审注。"
        "用户要求的来源、方法或适用限制说明仍应保留。",
        ("当前有效创作目标是本轮完整正文的实际成文范围：" if single else
            "当前有效创作目标只提供全篇背景，不要求本轮完成整篇：") + root,
        ("当前用户禁止使用个人或历史资料；只使用有效用户输入，不恢复旧简报或选项引用。"
         if not memory_allowed else "历史或当前事实仍需真实来源支持，候选与章节计划不构成事实依据。"),
        ("有效背景不增加未确认事实或授权；清空与假设保持原来源。" if single else
            "此前有效决定仅作为方向、风格、范围和事实边界约束，不是每章都要逐项重新展开的覆盖清单。") +
        "user_excluded 必须排除，agent_assumption 仍是未核验假设，不得写成用户事实：\n"
        + json.dumps(background, ensure_ascii=False),
        ("全部当前有效决定是本轮完整正文的成文和核验范围，source=user 的要求须实际落实；清空、排除与假设保持各自来源，不当作已确认事实。" if single else
            "本章已确认设计是本轮的实际成文和核验范围。") + "按文体、用途和篇幅体现选择；方法方案落实具体做法及取舍，"
        "叙事、语气、氛围和表达选择体现在正文写法中，不自行增设执行流程或验证栏目；不能只复述选项标签。"
        "保留参数、适用对象和不确定性，不编造背景、资源或效果：\n" + json.dumps(current, ensure_ascii=False),
        "后续章节标题只用于识别边界，仍未确认，连空标题和占位符也不得输出：\n"
        + json.dumps([{"id": item["id"], "title": item["title"]}
                      for item in chapter["outline"][chapter["index"]:]], ensure_ascii=False),
    ])


def chapter_title_key(value: str) -> str:
    # Existing Markdown cleanup can produce "## # 第一章 《标题》". Apply
    # presentation wrappers to a fixed point before comparing chapter identity.
    previous = None
    while value != previous:
        previous = value
        value = value.strip().strip("*").strip().strip('《》〈〉“”"\'「」').strip()
        value = re.sub(r"^(?:#{1,6}\s+)+", "", value)
        value = re.sub(r"^(?:第[一二三四五六七八九十百零〇\d]+(?:章|节|部分)|chapter\s+\d+|[一二三四五六七八九十百零〇\d]+[.、])"
                       r"\s*[：:·.\-]?\s*", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", "", value).casefold()


def _document_headings(document: str) -> List[str]:
    """ATX headings inside fenced examples are prose data, not chapter nodes."""
    headings = []
    fence = ""
    for line in document.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not marker.group(2).strip():
                fence = ""
            continue
        if marker:
            fence = marker.group(1)
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            headings.append(heading.group(1))
    return headings


def normalize_chapter_document(document: str, base: str, chapter: Dict[str, Any]) -> str:
    """Fold only redundant leading packaging headings, never previous prose."""
    prefix = base.rstrip()
    if prefix and not document.startswith(prefix):
        return document
    start = len(prefix)
    lines = document[start:].splitlines(keepends=True)
    result = []
    seen_current = False
    leading = True
    for line in lines:
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        current = heading and chapter_title_key(heading.group(2)) == chapter_title_key(chapter["title"])
        if leading and current:
            if seen_current:
                continue
            seen_current = True
        elif line.strip():
            leading = False
        result.append(line)
    return document[:start] + "".join(result)


def chapter_body_for_metrics(document: str, base: str, chapter: Dict[str, Any]) -> str:
    """Measure the current addition; prior chapters never consume its budget."""
    validate_chapter_document(document, base, chapter)
    addition = document[len(base.rstrip()):].lstrip()
    while addition:
        heading = re.match(r"^ {0,3}(#{1,6})\s+([^\n]+)(?:\n|$)", addition)
        if heading is None:
            break
        is_chapter_title = chapter_title_key(heading.group(2)) == chapter_title_key(chapter["title"])
        is_new_document_title = not base.strip() and heading.group(1) == "#"
        if not (is_chapter_title or is_new_document_title):
            break
        addition = addition[heading.end():].lstrip()
    return addition


def validate_chapter_document(document: str, base: str, chapter: Dict[str, Any],
                              *, completed: bool = False) -> None:
    """Guard every published mutation, plus the final chapter boundary."""
    prefix = base.rstrip()
    if prefix and not document.startswith(prefix):
        raise OperationError("CREATION_DOCUMENT_INVALID", "本章候选修改了已完成正文，已阻止覆盖；请重试本章")
    addition = document[len(prefix):].strip()
    headings = _document_headings(addition)
    for future in chapter["outline"][chapter["index"]:]:
        if any(chapter_title_key(future["title"]) == chapter_title_key(title) for title in headings):
            raise OperationError("CREATION_DOCUMENT_INVALID", "本章候选提前生成了未确认章节，已阻止发布；请重试本章")
    if sum(chapter_title_key(title) == chapter_title_key(chapter["title"]) for title in headings) > 1:
        raise OperationError("CREATION_DOCUMENT_INVALID", "当前章节标题重复，须修正后再交付")
    prose = re.sub(r"^\s{0,3}#{1,6}\s+.*$", "", addition, flags=re.MULTILINE).strip()
    if completed and (not prose or chapter["total"] != 1 and not any(
            chapter_title_key(chapter["title"]) == chapter_title_key(title) for title in headings)):
        raise OperationError("CREATION_DOCUMENT_INVALID", "本轮尚未完成当前章节的标题与正文，不能推进下一章")
