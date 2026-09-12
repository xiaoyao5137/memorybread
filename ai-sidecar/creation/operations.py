"""Generic, deterministic document operations. Natural language belongs to routing.

Selectors are grounded in the supplied document, never in topic-specific rules.
All edits bind to one immutable base and preserve every byte outside their ranges.
"""

import hashlib
import re
from typing import Any, Dict, List, Optional, Tuple
from model_schema import decoding_schema


class OperationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_operation_selectors(document: str, operation: Dict[str, Any]) -> Dict[str, Any]:
    """Repair JSON newline escaping only when a selector exactly matches the base.

    Literal backslashes already present in the document always win. No fuzzy
    matching, content changes, or scope expansion is permitted here.
    """
    def selector(value: Any) -> Any:
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            return value
        text = value["text"]
        if text in document:
            return value
        candidate = text
        for _ in range(2):
            candidate = candidate.replace(r"\\r\\n", "\r\n").replace(r"\r\n", "\r\n")
            candidate = candidate.replace(r"\\n", "\n").replace(r"\n", "\n")
            if candidate and candidate in document:
                return {**value, "text": candidate}
        return value
    result = validate_operation(operation)
    if "targets" in result:
        result["targets"] = [selector(target) for target in result["targets"]]
    if "patches" in result:
        result["patches"] = [{key: selector(value) if key in {"target", "destination"} else value
                              for key, value in patch.items()} for patch in result["patches"]]
    return result


def document_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


# The block names and termination rules mirror the CommonMark parser used by
# the desktop Markdown renderer. HTML contents are opaque to Markdown headings.
_HTML_BLOCK_TAGS = frozenset((
    "address article aside base basefont blockquote body caption center col colgroup "
    "dd details dialog dir div dl dt fieldset figcaption figure footer form frame "
    "frameset h1 h2 h3 h4 h5 h6 head header hr html iframe legend li link main menu "
    "menuitem nav noframes ol optgroup option p param search section summary table "
    "tbody td tfoot th thead title tr track ul"
).split())
_HTML_ATTRIBUTE = r'[a-zA-Z_:][a-zA-Z0-9_.:\-]*(?:\s*=\s*(?:[^\s"\'=<>`]+|\'[^\']*\'|"[^"]*"))?'
_HTML_COMPLETE_TAG = re.compile(
    r'^ {0,3}(?:</[a-zA-Z][a-zA-Z0-9-]*\s*>|<[a-zA-Z][a-zA-Z0-9-]*'
    r'(?:\s+' + _HTML_ATTRIBUTE + r')*\s*/?>)\s*$'
)


def _html_block_end(raw: str, in_paragraph: bool) -> Optional[str]:
    """Return an end pattern, or an empty pattern for blank-line termination."""
    opening = raw.lstrip(" ") if len(raw) - len(raw.lstrip(" ")) <= 3 else ""
    if re.match(r'<(?:pre|script|style|textarea)(?:[\s>]|$)', opening, re.I):
        return r'</(?:pre|script|style|textarea)\s*>'
    for prefix, end in (("<!--", "-->"), ("<?", r'\?>'), ("<![CDATA[", r'\]\]>')):
        if opening.startswith(prefix):
            return end
    if re.match(r'<![A-Z]', opening):
        return '>'
    tag = re.match(r'</?([a-zA-Z][a-zA-Z0-9-]*)(?:[\s/>]|$)', opening)
    if tag and tag[1].lower() in _HTML_BLOCK_TAGS:
        return ""
    if not in_paragraph and _HTML_COMPLETE_TAG.match(raw):
        return ""
    return None


def document_nodes(document: str) -> List[Dict[str, Any]]:
    """Top-level ATX/setext inventory, excluding code, HTML and containers.

    Node ranges include descendants until the next same/higher-level heading.
    Exact IDs disambiguate repeated titles without fuzzy matching.
    Lists and block quotes remain editable through exact text selectors.
    """
    headings = []
    offset = 0
    fence = ""
    fence_size = 0
    paragraph = []
    html_end = None
    container = None
    container_blank = False
    for line in re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', document):
        if not line:
            continue
        raw = line.rstrip("\r\n")
        if html_end is not None:
            if (html_end and re.search(html_end, raw, re.I)) or (not html_end and not raw.strip()):
                html_end = None
            paragraph = []
            offset += len(line)
            continue
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", raw)
        if fence:
            if marker and marker[1][0] == fence and len(marker[1]) >= fence_size and not marker[2].strip():
                fence = ""
            paragraph = []
            offset += len(line)
            continue
        atx = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$", raw)
        setext = re.match(r"^ {0,3}(=+|-+)[ \t]*$", raw)
        thematic = re.match(r'^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$', raw)
        container_start = re.match(r'^ {0,3}(>|(?:[-+*]|\d{1,9}[.)])(?:[ \t]+|$))', raw)
        if container is not None:
            indent = len(raw) - len(raw.lstrip(" \t"))
            nested = container > 0 and indent >= container
            lazy = (not container_blank and not atx and not thematic and not container_start
                    and not marker and _html_block_end(raw, True) is None)
            if not raw.strip() or nested or lazy:
                container_blank = not raw.strip()
                offset += len(line)
                continue
            container = None
        if container_start and not thematic and not (setext and paragraph):
            token = container_start[1].strip()
            interrupts = token == '>' or (raw[container_start.end():].strip() and
                                          (not token[0].isdigit() or token in {'1.', '1)'}))
            if not paragraph or interrupts:
                container = 0 if token == '>' else container_start.end()
                container_blank = False
                paragraph = []
                offset += len(line)
                continue
        if marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence, fence_size = marker[1][0], len(marker[1])
            paragraph = []
            offset += len(line)
            continue
        html_end = _html_block_end(raw, bool(paragraph))
        if html_end is not None:
            if html_end and re.search(html_end, raw, re.I):
                html_end = None
            paragraph = []
            offset += len(line)
            continue
        if atx:
            title = re.sub(r"(?:^|[ \t]+)#+[ \t]*$", "", atx[2] or "").strip()
            headings.append((offset, len(atx[1]), title))
            paragraph = []
        elif setext and paragraph:
            title = " ".join(item[1].strip() for item in paragraph)
            headings.append((paragraph[0][0], 1 if setext[1][0] == "=" else 2, title))
            paragraph = []
        else:
            block = re.match(r'^ {0,3}\[[^\]]+\]:', raw) if not paragraph else None
            if not raw.strip() or block or thematic or (not paragraph and raw.startswith(("    ", "\t"))):
                paragraph = []
            else:
                paragraph.append((offset, raw))
        offset += len(line)
    base = document_hash(document)
    nodes = []
    for index, (start, level, title) in enumerate(headings):
        end = next((entry[0] for entry in headings[index + 1:] if entry[1] <= level), len(document))
        nodes.append({"id": "node-" + hashlib.sha256((base + ":" + str(start)).encode()).hexdigest()[:16],
                      "title": title, "level": level, "start": start, "end": end})
    return nodes


def resolve_target(document: str, target: Any) -> Tuple[int, int]:
    _validate_target(target)
    if isinstance(target, dict) and set(target) == {"id"}:
        target = target["id"]
    if isinstance(target, str):
        matches = [node for node in document_nodes(document) if node["id"] == target]
        if len(matches) == 1:
            return matches[0]["start"], matches[0]["end"]
    elif isinstance(target, dict) and set(target) <= {"text", "occurrence"} and isinstance(target.get("text"), str) and target["text"]:
        literal = target["text"]
        # Lookaheads include overlapping occurrences ("aa" occurs twice in
        # "aaa"). A missing occurrence must never silently edit the first span.
        matches = re.finditer(r'(?=(' + re.escape(literal) + r'))', document)
        occurrence = target.get("occurrence")
        if occurrence is not None:
            for index, match in enumerate(matches, 1):
                if index == occurrence:
                    return match.span(1)
        else:
            first = next(matches, None)
            if first is not None:
                if next(matches, None) is not None:
                    raise OperationError("CREATION_TARGET_AMBIGUOUS", "目标出现多次，需要明确位置")
                return first.span(1)
    raise OperationError("CREATION_TARGET_MISSING", "目标不在当前文档中，请重新定位")


def _validate_target(target: Any) -> None:
    if isinstance(target, str) and target:
        return
    if isinstance(target, dict):
        if set(target) == {"id"} and isinstance(target["id"], str) and target["id"]:
            return
        if set(target) <= {"text", "occurrence"} and isinstance(target.get("text"), str) and target["text"]:
            if "occurrence" not in target or (type(target["occurrence"]) is int and target["occurrence"] >= 1):
                return
    raise OperationError("CREATION_OPERATION_INVALID", "选区必须是有效节点 ID 或原文片段，序号必须是正整数")


def _validate_patches(patches: Any) -> None:
    if not isinstance(patches, list) or not 1 <= len(patches) <= 64:
        raise OperationError("CREATION_OPERATION_INVALID", "需要 1 到 64 个文档操作")
    for patch in patches:
        if not isinstance(patch, dict):
            raise OperationError("CREATION_OPERATION_INVALID", "文档操作必须是对象")
        action = patch.get("action")
        if not isinstance(action, str):
            raise OperationError("CREATION_OPERATION_INVALID", "文档操作类型必须是字符串")
        required = {"delete": {"action", "target"}, "replace": {"action", "target", "content"},
                    "insert": {"action", "target", "content", "position"},
                    "move": {"action", "target", "destination", "position"}}.get(action)
        if required is None or set(patch) != required:
            raise OperationError("CREATION_OPERATION_INVALID", "文档操作字段缺失或不受支持")
        _validate_target(patch["target"])
        content = patch.get("content", "")
        if not isinstance(content, str) or len(content) > 120000:
            raise OperationError("CREATION_OPERATION_INVALID", "替换内容不合法或过长")
        if action in {"insert", "move"}:
            if not isinstance(patch["position"], str) or patch["position"] not in {"before", "after"}:
                raise OperationError("CREATION_OPERATION_INVALID", "插入位置必须是 before 或 after")
        if action == "move":
            _validate_target(patch["destination"])


def apply_patches(document: str, patches: Any, allowed_targets: Optional[List[Any]] = None) -> Tuple[str, Dict[str, Any]]:
    _validate_patches(patches)
    edits = []
    for patch in patches:
        action = patch["action"]
        start, end = resolve_target(document, patch["target"])
        content = patch.get("content", "")
        if action in {"delete", "replace"}:
            edits.append((start, end, "" if action == "delete" else content))
        elif action in {"insert", "move"}:
            position = patch.get("position")
            if action == "move":
                dest_start, dest_end = resolve_target(document, patch.get("destination"))
                if not (dest_end <= start or dest_start >= end):
                    raise OperationError("CREATION_PATCH_OVERLAP", "不能将目标移入自身范围")
                content = document[start:end]
                edits.append((start, end, ""))
                start, end = dest_start, dest_end
            anchor = start if position == "before" else end
            edits.append((anchor, anchor, content))
        else:
            raise OperationError("CREATION_OPERATION_INVALID", "不支持的文档操作")
    if allowed_targets is not None:
        allowed_ranges = [resolve_target(document, target) for target in allowed_targets]
        if any(not any(lower <= start <= end <= upper for lower, upper in allowed_ranges)
               for start, end, _ in edits):
            raise OperationError("CREATION_PATCH_OUT_OF_SCOPE", "局部修改超出了本轮选定范围")
    edits.sort(key=lambda edit: (edit[0], edit[1]))
    for left, right in zip(edits, edits[1:]):
        if right[0] < left[1] or (left[0] == left[1] == right[0] == right[1]):
            raise OperationError("CREATION_PATCH_OVERLAP", "多个操作范围重叠，请拆分或重新定位")
    result = document
    for start, end, content in reversed(edits):
        result = result[:start] + content + result[end:]
    changes = [{"change_type": "deleted" if not content else "added" if start == end else "modified",
                "section_title": "指定内容", "start_line": document.count("\n", 0, start) + 1,
                "end_line": document.count("\n", 0, end) + 1, "summary": "按本轮指令修改指定范围"}
               for start, end, content in edits]
    return result, {"operation": "document_patch", "base_hash": document_hash(document),
                    "result_hash": document_hash(result), "preserved_untouched": True,
                    "changes": changes, "change_count": len(changes),
                    "summary": "已完成 {} 处文档修改".format(len(changes))}


def validate_operation(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or raw.get("kind") not in {"patch", "transform", "generate", "respond", "answer", "resume", "undo", "execute_skill"}:
        raise OperationError("CREATION_OPERATION_INVALID", "缺少有效的本轮操作")
    kind = raw["kind"]
    fields = {"patch": {"patches"}, "transform": {"targets"}, "respond": {"response"},
              "resume": {"operation_id"}, "undo": {"operation_id"}, "execute_skill": {"skill_ids", "skill_assessments", "document_identity"},
              "generate": {"document_identity"}}
    if set(raw) - ({"kind", "constraint_skill_ids", "skill_assessments"} | fields.get(kind, set())):
        raise OperationError("CREATION_OPERATION_INVALID", "本轮操作包含不支持的字段")
    if kind == "patch":
        _validate_patches(raw.get("patches"))
    if kind == "transform":
        if not isinstance(raw.get("targets"), list) or not 1 <= len(raw["targets"]) <= 64:
            raise OperationError("CREATION_OPERATION_INVALID", "局部改写需要明确的目标范围")
        for target in raw["targets"]:
            _validate_target(target)
    if kind == "respond" and (not isinstance(raw.get("response"), str) or not raw["response"].strip() or len(raw["response"]) > 2000):
        raise OperationError("CREATION_OPERATION_INVALID", "直接回应不能为空")
    if kind in {"resume", "undo"} and (not isinstance(raw.get("operation_id"), str) or not raw["operation_id"].strip()):
        raise OperationError("CREATION_RESUME_MISSING", "需要指定未完成操作")
    if kind == "execute_skill" and (not isinstance(raw.get("skill_ids"), list) or not raw["skill_ids"]):
        raise OperationError("CREATION_OPERATION_INVALID", "需要明确 Skill 标识")
    for key in ("skill_ids", "constraint_skill_ids"):
        if key in raw and (not isinstance(raw[key], list) or any(not isinstance(item, str) or not item.strip() for item in raw[key])):
            raise OperationError("CREATION_OPERATION_INVALID", "Skill 标识必须是非空字符串列表")
    return {key: raw[key] for key in ("kind", "patches", "targets", "response", "operation_id", "skill_ids", "constraint_skill_ids", "document_identity", "skill_assessments") if key in raw}


def routing_response_schema(tool_ids: List[str], agent_ids: List[str], skill_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """The decoding contract describes operations, never an execution sequence."""
    selector = {"anyOf": [
        {"type": "string", "minLength": 1},
        {"type": "object", "properties": {"id": {"type": "string", "minLength": 1}}, "required": ["id"], "additionalProperties": False},
        {"type": "object", "properties": {"text": {"type": "string", "minLength": 1}, "occurrence": {"type": "integer", "minimum": 1}},
         "required": ["text"], "additionalProperties": False},
    ]}
    patch_variants = []
    for action, required in (("delete", []), ("replace", ["content"]), ("insert", ["content", "position"]), ("move", ["destination", "position"])):
        # A text replacement binds an exact text span. A subtree ID is easy to
        # mistake for its heading or body and can silently erase descendants.
        properties = {"action": {"const": action}, "target": selector["anyOf"][2] if action == "replace" else selector}
        for key in required:
            properties[key] = (selector if key == "destination" else {"enum": ["before", "after"]}
                               if key == "position" else {"type": "string"})
        patch_variants.append({"type": "object", "properties": properties,
                               "required": ["action", "target"] + required, "additionalProperties": False})
    fields = {"patches": {"type": "array", "minItems": 1, "maxItems": 64, "items": {"anyOf": patch_variants}},
              "targets": {"type": "array", "minItems": 1, "maxItems": 64, "items": selector},
              "response": {"type": "string", "minLength": 1, "maxLength": 2000},
              "operation_id": {"type": "string", "minLength": 1},
              "skill_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}}}
    operations = []
    for kind, required in (("patch", ["patches"]), ("transform", ["targets"]), ("generate", []),
                           ("respond", ["response"]), ("answer", []), ("resume", ["operation_id"]),
                           ("undo", ["operation_id"]), ("execute_skill", ["skill_ids"])):
        if kind == "execute_skill" and skill_ids == []:
            continue
        properties = {"kind": {"const": kind}, **{key: fields[key] for key in required}}
        skill_items = {"enum": skill_ids} if skill_ids else {"type": "string", "minLength": 1}
        if kind == "execute_skill":
            properties["skill_ids"] = {"type": "array", "minItems": 1, "items": skill_items}
        if kind in {"generate", "execute_skill"}:
            from .skill_governance import IDENTITY_SCHEMA, ASSESSMENT_SCHEMA
            properties["document_identity"] = IDENTITY_SCHEMA
            required = required + ["document_identity"]
            if kind == "execute_skill":
                properties["skill_assessments"] = ASSESSMENT_SCHEMA
                required = required + ["skill_assessments"]
        from .skill_governance import ASSESSMENT_SCHEMA
        properties["skill_assessments"] = ASSESSMENT_SCHEMA
        properties["constraint_skill_ids"] = {"type": "array", "items": skill_items}
        if skill_ids == []:
            properties["constraint_skill_ids"]["maxItems"] = 0
        operations.append({"type": "object", "properties": properties, "required": ["kind"] + required,
                           "additionalProperties": False})
    def capability_array(ids: List[str]) -> Dict[str, Any]:
        return ({"type": "array", "items": {"enum": ids}, "uniqueItems": True}
                if ids else {"type": "array", "maxItems": 0})
    return {"type": "object", "properties": {"reasoning": {"type": "string", "maxLength": 400},
        "operation": {"anyOf": operations}, "tools": capability_array(tool_ids), "agents": capability_array(agent_ids)},
        "required": ["reasoning", "operation", "tools", "agents"], "additionalProperties": False}


def patch_response_schema() -> Dict[str, Any]:
    """Reuse the operation contract for authored local edits."""
    variants = routing_response_schema([], [], [])["properties"]["operation"]["anyOf"]
    patch = next(item for item in variants if item["properties"]["kind"]["const"] == "patch")
    return decoding_schema({"type": "object", "properties": {"patches": patch["properties"]["patches"]},
                            "required": ["patches"], "additionalProperties": False})


def validate_literal_patch(document: str, patches: List[Dict[str, Any]], instruction: str) -> None:
    """A direct patch may copy explicit replacement text, not invent new wording.

    Compare only changed spans, so replacing a word inside an otherwise unchanged
    sentence does not require the user to repeat the full sentence.
    """
    from difflib import SequenceMatcher
    for patch in patches:
        if patch.get("action") not in {"replace", "insert"}:
            continue
        if patch["action"] == "replace" and not (isinstance(patch.get("target"), dict) and "text" in patch["target"]):
            raise OperationError("CREATION_OPERATION_INVALID", "精确替换必须用 text 选择器定位原文片段；章节节点包含标题和全部子内容，不能用节点 ID 替换局部文字")
        start, end = resolve_target(document, patch.get("target"))
        before = document[start:end] if patch["action"] == "replace" else ""
        after = patch.get("content", "")
        for tag, _, _, lower, upper in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
            added = after[lower:upper].strip()
            if tag in {"insert", "replace"} and added and added not in instruction:
                raise OperationError("CREATION_OPERATION_INVALID", "补丁含用户未直接提供的新措辞；请使用 transform 并保持指定范围的原有结构")
