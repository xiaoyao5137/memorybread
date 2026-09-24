"""Generic, deterministic document operations. Natural language belongs to routing.

Selectors are grounded in the supplied document, never in topic-specific rules.
All edits bind to one immutable base and preserve every byte outside their ranges.
"""

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from model_schema import decoding_schema


class OperationError(ValueError):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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


APPEND_INSTRUCTION_MARKERS = (
    "补充", "新增", "增加", "添加", "加入", "写入", "纳入", "加上", "完善", "扩展", "补上",
)


def bind_append_transform_scope(
    instruction: str,
    document: str,
    operation: Dict[str, Any],
) -> Dict[str, Any]:
    """Ground append transforms before either routing or execution validation."""
    if (operation.get("kind") != "transform" or not document.strip()
            or not any(marker in instruction for marker in APPEND_INSTRUCTION_MARKERS)):
        return operation

    def normalized(value: str) -> str:
        return re.sub(r"[\s：:、，,。.!！?？（）()《》“”\"'`#_-]+", "", value).lower()

    message = normalized(instruction)
    nodes = document_nodes(document)
    named = [node for node in nodes if node.get("level", 0) >= 2
             and normalized(str(node.get("title") or "")) in message]
    # A named section already includes all of its descendant headings. Keeping
    # both the ancestor and descendants creates overlapping edit scopes and can
    # tempt a writer to address only the small child nodes. Bind the smallest
    # set of outer named sections instead. This also keeps a repeated generic
    # heading such as ``待确认事项`` inside the explicitly named parent section,
    # rather than accidentally opening every same-titled section for editing.
    outer_named = [
        node for node in named
        if not any(
            other["start"] <= node["start"] and node["end"] <= other["end"]
            and other["id"] != node["id"]
            for other in named
        )
    ]
    contained_titles = {
        normalized(str(child.get("title") or ""))
        for parent in outer_named
        for child in named
        if parent["id"] != child["id"]
        and parent["start"] <= child["start"] and child["end"] <= parent["end"]
    }
    outer_named = [
        node for node in outer_named
        if normalized(str(node.get("title") or "")) not in contained_titles
    ]
    targets = [node["id"] for node in outer_named]
    if not targets:
        root = next((node for node in nodes if node.get("level") == 1), None)
        if root:
            targets = [root["id"]]
    return {**operation, "targets": targets} if targets else operation


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
        # When the selected literal exists as a complete line, prefer those
        # spans over shorter substring matches. This keeps selectors such as
        # "- " bound to an empty Markdown list item instead of the prefix of
        # an unrelated populated item. Multi-line selections retain their
        # original byte-exact substring semantics.
        line_matches = []
        if "\n" not in literal and "\r" not in literal:
            line_matches = list(re.finditer(
                r"(?m)^(" + re.escape(literal) + r")(?=\r?(?:\n|\Z))", document
            ))
        # Lookaheads include overlapping occurrences ("aa" occurs twice in
        # "aaa"). A missing occurrence must never silently edit the first span.
        matches = iter(line_matches) if line_matches else re.finditer(
            r'(?=(' + re.escape(literal) + r'))', document
        )
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


def target_resolves(document: str, target: Any) -> bool:
    try:
        resolve_target(document, target)
        return True
    except OperationError:
        return False


def repair_generated_literal_selectors(
    document: str,
    patches: List[Dict[str, Any]],
    allowed_targets: List[Any],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Repair uniquely provable line-copy drift in generated selectors.

    A selector is rebound only when one same-line-count window inside the
    allowed scope matches after horizontal-space changes or removal of a short
    trailing parenthetical label. Ambiguous or substantive drift still fails.
    """
    allowed_ranges = [resolve_target(document, target) for target in allowed_targets]
    lines = []
    offset = 0
    for raw in re.findall(r'[^\r\n]*(?:\r\n|\r|\n|$)', document):
        if not raw:
            continue
        lines.append((offset, offset + len(raw), raw))
        offset += len(raw)

    def in_scope(start: int, end: int) -> bool:
        return any(scope_start <= start and end <= scope_end
                   for scope_start, scope_end in allowed_ranges)

    def canonical_line(value: str) -> str:
        return re.sub(r"[ \t]+", "", value.rstrip("\r\n"))

    def line_matches(source: str, candidate: str) -> bool:
        source_key = canonical_line(source)
        candidate_key = canonical_line(candidate)
        if source_key == candidate_key:
            return True
        without_note = re.sub(
            r"(?:（[^（）\r\n]{1,24}）|\([^()\r\n]{1,24}\))$", "", source_key
        )
        return without_note == candidate_key

    repaired = []
    changed = False
    for patch in patches:
        item = dict(patch)
        for key in ("target", "destination"):
            selector = item.get(key)
            if not (isinstance(selector, dict) and isinstance(selector.get("text"), str)
                    and selector["text"] and not target_resolves(document, selector)):
                continue
            wanted = selector["text"]
            wanted_lines = wanted.rstrip("\r\n").splitlines()
            if not wanted_lines:
                continue
            candidates = []
            for index in range(0, len(lines) - len(wanted_lines) + 1):
                window = lines[index:index + len(wanted_lines)]
                start = window[0][0]
                raw_end = window[-1][1]
                last = window[-1][2]
                end = raw_end if wanted.endswith(("\n", "\r")) else raw_end - len(last) + len(last.rstrip("\r\n"))
                if in_scope(start, end) and all(
                    line_matches(source, actual[2])
                    for source, actual in zip(wanted_lines, window)
                ):
                    candidates.append(document[start:end])
            if len(candidates) == 1:
                item[key] = {**selector, "text": candidates[0]}
                changed = True
        repaired.append(item)

    spans = []
    for index, patch in enumerate(repaired):
        try:
            spans.append((index, *resolve_target(document, patch.get("target"))))
        except OperationError:
            continue
    redundant = set()
    for delete_index, delete_start, delete_end in spans:
        delete_patch = repaired[delete_index]
        if delete_patch.get("action") != "delete":
            continue
        deleted_text = document[delete_start:delete_end]
        for replace_index, replace_start, replace_end in spans:
            replace_patch = repaired[replace_index]
            if (replace_patch.get("action") == "replace"
                    and replace_start <= delete_start and delete_end <= replace_end
                    and deleted_text not in str(replace_patch.get("content") or "")):
                redundant.add(delete_index)
                changed = True
                break
    return [patch for index, patch in enumerate(repaired) if index not in redundant], changed


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


def normalize_generated_patch_markdown(
    patches: Any, document: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Restore a block boundary when generated patch text glues on a heading.

    This is intentionally limited to model-generated patch content. Literal
    user patches continue to pass through apply_patches byte-for-byte. Fenced
    code is protected because examples may contain heading markers as data.
    """
    normalized = []
    for patch in patches:
        current = dict(patch)
        content = current.get("content")
        if isinstance(content, str):
            parts = re.split(r"(```.*?(?:```|$)|~~~.*?(?:~~~|$))", content, flags=re.S)
            for index in range(0, len(parts), 2):
                parts[index] = re.sub(
                    r"(?m)(?<=[^\n\\#])(#{2,6}[ \t]+\S)", r"\n\n\1", parts[index]
                )
            content = "".join(parts)
            if document is not None and current.get("action") == "insert":
                start, end = resolve_target(document, current.get("target"))
                anchor = start if current.get("position") == "before" else end
                if (anchor > 0 and document[anchor - 1] not in "\r\n"
                        and re.match(r"^#{1,6}[ \t]+\S", content)):
                    content = "\n\n" + content
                if (anchor < len(document) and document[anchor] not in "\r\n"
                        and content and content[-1] not in "\r\n"):
                    content += "\n\n"
            current["content"] = content
        normalized.append(current)
    return normalized


def rebind_generated_append_target(
    patches: Any,
    document: str,
    allowed_targets: Any,
    instruction: str,
) -> List[Dict[str, Any]]:
    """Recover an insert that incorrectly targets the heading it is creating.

    This fallback is deliberately narrow: the user asked to append, the only
    allowed scope is the root node spanning the complete document, and the
    generated selector does not exist.  Appending the fragment after that root
    preserves every original byte.  Other missing or ambiguous selectors still
    fail closed.
    """
    result = [dict(item) for item in patches] if isinstance(patches, list) else patches
    if (not isinstance(result, list)
            or not any(marker in instruction for marker in APPEND_INSTRUCTION_MARKERS)
            or not isinstance(allowed_targets, list) or len(allowed_targets) != 1):
        return result
    try:
        allowed_start, allowed_end = resolve_target(document, allowed_targets[0])
    except OperationError:
        return result
    if allowed_start != 0 or allowed_end != len(document):
        return result
    for patch in result:
        if not isinstance(patch, dict) or patch.get("action") != "insert":
            continue
        try:
            resolve_target(document, patch.get("target"))
        except OperationError as error:
            if error.code != "CREATION_TARGET_MISSING":
                raise
            patch["target"] = allowed_targets[0]
            patch["position"] = "after"
    return result


def generated_patch_problems(
    document: str, patches: Any, instruction: str = ""
) -> List[str]:
    """Reject generated inserts that smuggle a rewritten document as a delta.

    A transform insert must contain the new fragment only.  Model output can
    occasionally place a near-complete copy of the selected document inside an
    insert, which preserves the old bytes technically but duplicates the whole
    body.  Compare substantive existing lines before applying the patch so this
    cannot be approved later by a semantic delivery reviewer.
    """
    permits_copy = bool(re.search(
        r"复制|拷贝|重复一遍|原样再放|duplicate|copy", instruction, re.I
    ))
    existing = {
        line.strip()
        for line in document.splitlines()
        if len(line.strip()) >= 8
    }
    for patch in patches if isinstance(patches, list) else []:
        if not isinstance(patch, dict) or patch.get("action") != "insert":
            continue
        content = patch.get("content")
        if not isinstance(content, str):
            continue
        repeated = [
            line.strip()
            for line in content.splitlines()
            if len(line.strip()) >= 8 and line.strip() in existing
        ]
        repeated_chars = sum(len(line) for line in repeated)
        if not permits_copy and len(set(repeated)) >= 2 and repeated_chars >= 32:
            return ["insert_repeats_existing_content"]
    return []


def recover_generated_insert_supersequence(
    document: str,
    patches: Any,
    allowed_targets: Any,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Convert a copied target plus insertions back into one atomic replace.

    Small local models sometimes obey the requested edit semantically but put
    the complete selected node in an ``insert`` payload.  Applying that payload
    would duplicate the original, while simply rejecting it loses a recoverable
    result.  Recovery is safe only when the candidate contains every byte of the
    selected target, in order, and differs exclusively by inserted bytes.  Any
    deletion, rewrite, ambiguous scope, or multi-patch response still fails
    closed through the normal validators.
    """
    result = [dict(item) for item in patches] if isinstance(patches, list) else patches
    if (not isinstance(result, list) or len(result) != 1
            or not isinstance(allowed_targets, list) or len(allowed_targets) != 1):
        return result, False
    patch = result[0]
    content = patch.get("content") if isinstance(patch, dict) else None
    if patch.get("action") != "insert" or not isinstance(content, str):
        return result, False
    try:
        target_start, target_end = resolve_target(document, patch.get("target"))
        allowed_start, allowed_end = resolve_target(document, allowed_targets[0])
    except OperationError:
        return result, False
    if (target_start, target_end) != (allowed_start, allowed_end):
        return result, False
    original = document[target_start:target_end]
    if not original or content == original:
        return result, False
    matcher = SequenceMatcher(None, original, content, autojunk=False)
    opcodes = matcher.get_opcodes()
    if (not opcodes or any(tag not in {"equal", "insert"} for tag, *_ in opcodes)
            or "".join(original[i1:i2] for tag, i1, i2, _, _ in opcodes
                         if tag == "equal") != original
            or not any(tag == "insert" and j1 != j2
                       for tag, _, _, j1, j2 in opcodes)):
        return result, False
    return [{"action": "replace", "target": allowed_targets[0], "content": content}], True


def rebind_targets_to_document(
    source_document: str,
    target_document: str,
    targets: Any,
) -> Optional[List[Any]]:
    """Map an allowed selector scope to a newer candidate document.

    Delivery repair normally regenerates a patch from the immutable input
    baseline.  If the model instead selects text introduced by the rejected
    candidate, the repair can still be applied safely to that candidate when
    the original allowed nodes have a unique title/level counterpart.  Literal
    selectors already present in the candidate retain their exact semantics.
    """
    if not isinstance(targets, list):
        return None
    source_nodes = document_nodes(source_document)
    target_nodes = document_nodes(target_document)
    rebound: List[Any] = []
    for target in targets:
        try:
            resolve_target(target_document, target)
            rebound.append(target)
            continue
        except OperationError:
            pass
        node_id = target.get("id") if isinstance(target, dict) else target
        source_node = next((item for item in source_nodes if item["id"] == node_id), None)
        if source_node is None:
            return None
        source_matches = [item for item in source_nodes
                          if item["level"] == source_node["level"]
                          and item["title"] == source_node["title"]]
        target_matches = [item for item in target_nodes
                          if item["level"] == source_node["level"]
                          and item["title"] == source_node["title"]]
        if len(source_matches) != len(target_matches):
            return None
        ordinal = source_matches.index(source_node)
        rebound.append(target_matches[ordinal]["id"])
    return rebound


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
