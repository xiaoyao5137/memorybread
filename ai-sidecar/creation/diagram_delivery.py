"""Delivery contract for one standalone, editable Mermaid diagram."""

import hashlib
import re
from collections import OrderedDict
from typing import Any, Dict, Tuple


_FENCED = re.compile(r"\A```mermaid[ \t]*\r?\n([\s\S]*?)\r?\n```\s*\Z", re.IGNORECASE)
_HEADER = re.compile(
    r"\A(?:flowchart\s+(?:TB|TD|BT|RL|LR)|graph\s+(?:TB|TD|BT|RL|LR)|"
    r"sequenceDiagram|stateDiagram-v2|classDiagram|erDiagram|journey|mindmap)\b",
    re.IGNORECASE,
)
_RELATION = re.compile(r"(?:<-->|-->|---|-\.->|==>|->>|-->>|\|\|--|\[\*\]\s*-->)")
_UNSAFE = re.compile(r"(?im)^\s*(?:click\b|linkStyle\b|%%\{init\s*:)|<\s*(?:script|iframe)\b")
_PLACEHOLDER = re.compile(r"对象\s*[AB]|客户端\s*-->\s*核心服务|核心服务\s*-->\s*数据存储")
_FLOW_HEADER = re.compile(r"^(?:flowchart|graph)\s+(?:TB|TD|BT|RL|LR)$", re.IGNORECASE)
_FLOW_NODE = re.compile(r'^([A-Za-z][A-Za-z0-9_]*)\["([^"\[\]<>\r\n]{1,100})"\]$')
_FLOW_SUBGRAPH = re.compile(r'^subgraph\s+([A-Za-z][A-Za-z0-9_]*)\["([^"\[\]<>\r\n]{1,80})"\]$')
_FLOW_EDGE = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*)\s+(<-->|-->|-\.->|==>|---)(?:\|([^|<>\r\n]{1,80})\|)?\s+([A-Za-z][A-Za-z0-9_]*)$"
)
_MERMAID_RESERVED = {"end", "graph", "flowchart", "class", "style", "subgraph", "click"}
_EDGE_SOURCE = re.compile(r"^(.+?)\s*(<-->|-->|-\.->|==>|---)\s*(?:\|([^|]+)\|\s*)?(.+)$")
_NODE_REF = re.compile(
    r"^([A-Za-z][A-Za-z0-9_]*)(\[[^\[\]\r\n]+\]|\(\([^\r\n]+\)\)|"
    r"\([^()\r\n]+\)|\{[^{}\r\n]+\})?$"
)
_SOURCE_SUBGRAPH = re.compile(
    r"^subgraph\s+([A-Za-z][A-Za-z0-9_]*)\s*\[([^\[\]\r\n]+)\]$",
    re.IGNORECASE,
)


def _validate_flowchart(lines: list[str]) -> None:
    """Keep generated flowcharts inside a small syntax subset the UI can render."""
    if len(lines) > 100:
        raise ValueError("图示节点过多")
    nodes = set()
    edges = []
    groups = set()
    open_group = False
    for line in lines[1:]:
        value = line.strip()
        group = _FLOW_SUBGRAPH.fullmatch(value)
        if group:
            identifier = group.group(1)
            if (open_group or identifier.lower() in _MERMAID_RESERVED
                    or identifier in groups or identifier in nodes or len(groups) >= 8):
                raise ValueError("图示分组无效或嵌套过深")
            groups.add(identifier)
            open_group = True
            continue
        if value == "end":
            if not open_group:
                raise ValueError("图示分组结束位置无效")
            open_group = False
            continue
        node = _FLOW_NODE.fullmatch(value)
        if node:
            identifier = node.group(1)
            if identifier.lower() in _MERMAID_RESERVED or identifier in nodes or identifier in groups:
                raise ValueError("图示节点标识无效或重复")
            nodes.add(identifier)
            continue
        edge = _FLOW_EDGE.fullmatch(value)
        if edge:
            edges.append((edge.group(1), edge.group(4)))
            continue
        raise ValueError("flowchart 含有不支持的 Mermaid 语法；节点须单独声明并使用双引号标签")
    if open_group:
        raise ValueError("图示分组没有结束")
    if len(nodes) < 2 or not edges:
        raise ValueError("图示缺少实际节点关系")
    if any(start not in nodes or end not in nodes for start, end in edges):
        raise ValueError("图示关系引用了未声明节点")


def _safe_label(value: str) -> str:
    value = value.strip()
    wrappers = (("[(", ")]"), ("((", "))"), ("[", "]"),
                ("(", ")"), ("{", "}"), ('"', '"'), ("'", "'"))
    for _ in range(2):
        matched = False
        for opening, closing in wrappers:
            if value.startswith(opening) and value.endswith(closing):
                value = value[len(opening):-len(closing)]
                matched = True
                break
        if not matched:
            break
    value = re.sub(r"(?i)<br\s*/?>", " ", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.strip()
    value = re.sub(r"\s+", " ", value)
    return value.replace('"', '“').replace("[", "［").replace("]", "］").replace("|", "｜")[:100]


def _read_node_ref(value: str) -> tuple[str, str]:
    value = value.strip()
    if re.search(r"\s&\s|\]\s*&|&\s*[A-Za-z][A-Za-z0-9_]*", value):
        raise ValueError("图示连线不能合并多个节点")
    match = _NODE_REF.fullmatch(value)
    if not match:
        raise ValueError("图示关系节点无法解析")
    identifier, suffix = match.groups()
    return identifier, _safe_label(suffix) if suffix else ""


def _read_subgraph_header(line: str, ordinal: int) -> tuple[str, str]:
    """Normalize common Mermaid group headers without turning titles into nodes."""
    match = _SOURCE_SUBGRAPH.fullmatch(line)
    if match:
        identifier, raw_label = match.groups()
    else:
        raw = line[len("subgraph "):].strip()
        if not raw or any(token in raw for token in ("-->", "---", "==>", ";")):
            raise ValueError("图示分组标题无法安全解析")
        # Mermaid writers often use a Chinese group name as the identifier, or
        # put brackets inside a quoted label. Keep the visible label and assign
        # our restricted ASCII identifier; group IDs are not edge endpoints.
        bracket = re.fullmatch(r'([^\[\]\r\n]+)\[("[^"\r\n]+"|[^\[\]\r\n]+)\]', raw)
        if bracket:
            source_id, raw_label = bracket.groups()
            source_id = source_id.strip()
            if not source_id or re.search(r'[<>;"\r\n]', source_id):
                raise ValueError("图示分组标题无法安全解析")
            identifier = (source_id if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", source_id)
                          else "DiagramGroup{}".format(ordinal))
        elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", raw):
            identifier, raw_label = raw, raw
        else:
            if "[" in raw or "]" in raw:
                raise ValueError("图示分组标题无法安全解析")
            identifier, raw_label = "DiagramGroup{}".format(ordinal), raw
    label = _safe_label(raw_label)
    if (not label or len(label) > 80 or identifier.lower() in _MERMAID_RESERVED
            or re.search(r"(?i)<\s*(?:script|iframe)\b", raw_label)):
        raise ValueError("图示分组标题无效")
    return identifier, label


def canonicalize_standalone_diagram(result: str, title: str = "") -> str:
    """Convert flowchart notation without inventing semantic nodes or edges.

    ``title`` is retained for callers that also store document identity. Mermaid
    flowcharts have no native visible title in this restricted syntax, so a
    title cannot be added as a graph node: that would assert a false relation.
    """
    value = str(result or "").strip()
    # Preserve already valid groups. Flattening their labels into node names
    # would erase the intended visual distinction between source stages.
    try:
        return normalize_standalone_diagram(value)[0]
    except ValueError:
        pass
    fenced = _FENCED.fullmatch(value)
    code = fenced.group(1).strip() if fenced else value
    # Decode only the escaped quote used by JSON-shaped Mermaid responses.
    # Other escapes remain literal so a malformed graph cannot gain new edges.
    code = code.replace('\\"', '"')
    lines = [line.strip() for line in code.splitlines() if line.strip()]
    if not lines or not _FLOW_HEADER.fullmatch(lines[0]):
        return normalize_standalone_diagram(result)[0]
    nodes: "OrderedDict[str, str]" = OrderedDict()
    edges: list[tuple[str, str, str, str]] = []
    groups: "OrderedDict[str, str]" = OrderedDict()
    membership: dict[str, str] = {}
    current_group = ""

    def remember(identifier: str, label: str, *, declaration: bool = False) -> None:
        if identifier in groups:
            raise ValueError("图示分组标识不能作为连线节点；请连接组内已声明的节点")
        if identifier.lower() in _MERMAID_RESERVED:
            raise ValueError("图示节点标识无效")
        had_label = bool(nodes.get(identifier))
        if label:
            if identifier in nodes and nodes[identifier] not in {"", label}:
                raise ValueError("同一图示节点出现冲突标签")
            nodes[identifier] = label
        elif identifier not in nodes:
            nodes[identifier] = ""
        # A bare edge endpoint refers to an existing node, even when the edge
        # appears inside a different group. An inline label establishes group
        # membership only when it first declares that node.
        if current_group and (declaration or (label and not had_label)):
            prior_group = membership.get(identifier, "")
            if prior_group and prior_group != current_group:
                raise ValueError("同一节点不能属于多个图示分组")
            membership[identifier] = current_group

    for line in lines[1:]:
        if line.startswith("%%"):
            if line.startswith("%%{"):
                raise ValueError("图示包含不允许的初始化指令")
            continue
        if re.match(r"(?i)^subgraph\s+", line):
            if current_group or len(groups) >= 8:
                raise ValueError("图示分组不能嵌套或过多")
            group_id, group_label = _read_subgraph_header(line, len(groups) + 1)
            if group_id in groups or group_id in nodes:
                raise ValueError("图示分组标识重复")
            groups[group_id] = group_label
            current_group = group_id
            continue
        if line == "end":
            if not current_group:
                raise ValueError("图示分层语法不完整")
            current_group = ""
            continue
        if line.lower().startswith("title "):
            continue
        if re.match(r"(?i)^(?:style|classDef|class|linkStyle|direction)\b", line):
            # These affect presentation only. The delivery subset omits them,
            # while the source-backed nodes, edges and group labels survive.
            continue
        if re.search(r"(?i)<\s*(?:script|iframe)\b", line):
            raise ValueError("图示包含不允许的 HTML")
        edge = _EDGE_SOURCE.fullmatch(line)
        if edge:
            start, start_label = _read_node_ref(edge.group(1))
            end, end_label = _read_node_ref(edge.group(4))
            remember(start, start_label)
            remember(end, end_label)
            edges.append((start, end, edge.group(2), _safe_label(edge.group(3) or "")))
            continue
        identifier, label = _read_node_ref(line)
        if not label:
            raise ValueError("图示节点缺少可见标签")
        remember(identifier, label, declaration=True)
    if current_group:
        raise ValueError("图示分层语法不完整")
    if any(not label for label in nodes.values()):
        raise ValueError("图示包含未命名节点")
    if any(group_id not in membership.values() for group_id in groups):
        raise ValueError("图示包含空分组")
    output = [lines[0]]
    output.extend('    {}["{}"]'.format(identifier, label)
                  for identifier, label in nodes.items() if identifier not in membership)
    for group_id, group_label in groups.items():
        output.append('    subgraph {}["{}"]'.format(group_id, group_label))
        output.extend('        {}["{}"]'.format(identifier, label)
                      for identifier, label in nodes.items()
                      if membership.get(identifier) == group_id)
        output.append("    end")
    output.extend('    {} {}{} {}'.format(start, relation,
                  '|{}|'.format(label) if label else '', end)
                  for start, end, relation, label in edges)
    normalized, _ = normalize_standalone_diagram("```mermaid\n" + "\n".join(output) + "\n```")
    return normalized


def _fallback_label(value: str) -> str:
    """Keep only a literal source phrase, without list numbering or effects."""
    value = re.sub(r"^\s*(?:[●○■•-]\s*|(?:\d+|[a-z])[.、)]\s*)", "", value, flags=re.I)
    value = value.strip(" \t\r\n:：，,。；;、")
    value = re.sub(r"\s+", " ", value)
    return value


def _fallback_span_edges(text: str) -> list[tuple[str, str]]:
    """Recognize a small set of local grammatical relations, never cooccurrence."""
    edges: list[tuple[str, str]] = []
    # A source may wrap one explicit arrow sequence inside words. Rejoin only
    # adjacent continuation lines; headings and ended sentences stay separate.
    text = re.sub(r"(?<=闭)\n(?=环)", "", text)
    text = re.sub(
        r"(?<![。；;，,])\n(?!(?:[●○■]|KR\d+|\d+[.、]))(?=[^\n]*(?:➡\ufe0f?|→|->|—))",
        "", text, flags=re.I,
    )
    for line in text.splitlines():
        if re.search(r"➡\ufe0f?|→|->", line):
            chain = line.rsplit("：", 1)[-1].rsplit(":", 1)[-1]
            parts = [_fallback_label(part) for part in
                     re.split(r"\s*(?:➡\ufe0f?|→|->)\s*", chain)]
            if 3 <= len(parts) <= 9 and all(2 <= len(part) <= 50 for part in parts):
                edges.extend(zip(parts, parts[1:]))
        if line.count("—") >= 3 and "闭环" in line:
            chain = line.rsplit("：", 1)[-1].rsplit(":", 1)[-1]
            chain = re.split(r"[，,。；;]", chain, 1)[0]
            parts = [_fallback_label(part) for part in chain.split("—")]
            parts[-1] = re.sub(r"的闭环.*$", "", parts[-1]).strip()
            if parts and re.search(r"[A-Za-z][A-Za-z0-9_-]*$", parts[0]):
                parts[0] = re.search(r"[A-Za-z][A-Za-z0-9_-]*$", parts[0]).group(0)
            if 4 <= len(parts) <= 9 and all(2 <= len(part) <= 50 for part in parts):
                edges.extend(zip(parts, parts[1:]))
    for match in re.finditer(
        r"([^\n，,。；;:：]{2,48})[，,]\s*(?:沉淀|打通|调用|连接|接入|串联)"
        r"([^\n，,。；;]{2,60})", text,
    ):
        edges.append((_fallback_label(match.group(1)), _fallback_label(match.group(2))))

    for match in re.finditer(
        r"([^\n，,。；;:：]{2,48}?)\s+实现([^\n，,。；;]{2,80})", text,
    ):
        left = _fallback_label(match.group(1))
        if re.search(r"(?:无法|不能|未能|尚未|难以|不可能)$", left):
            continue
        targets = re.sub(r"等.{0,24}(?:功能|能力).*?$", "", match.group(2)).strip()
        for target in re.split(r"[、及和]", targets):
            right = _fallback_label(target)
            if right:
                edges.append((left, right))

    # Explicit resource/application relations in prose are useful when the
    # source has no arrow diagram. Keep literal phrases from one local clause.
    for match in re.finditer(
        r"(?:将|把)([^\n，,。；;:：]{2,32}?)提供给"
        r"([^\n，,。；;:：]{2,32}?)(?:使用|调用|消费)", text,
    ):
        edges.append((_fallback_label(match.group(1)),
                      _fallback_label(match.group(2))))
    for match in re.finditer(
        r"在([\u4e00-\u9fffA-Za-z0-9]{2,20})[，,]\s*"
        r"(?:直接)?使用\s*([^\n，,。；;]{2,38})", text,
    ):
        resource = re.split(r"(?:完成|构建|运行|用于)", match.group(2), 1)[0]
        edges.append((_fallback_label(match.group(1)),
                      _fallback_label(resource)))
    for match in re.finditer(
        r"推进[“\"]?([^”\"，,。；;\n]{2,30})[”\"]?[，,]"
        r"[^。；;\n]{0,24}?完成([^。；;\n]{2,100}?)的拉通", text,
    ):
        plan = _fallback_label(match.group(1))
        for component in re.split(r"[、及和]", match.group(2)):
            label = _fallback_label(component)
            if label:
                edges.append((plan, label))

    for match in re.finditer(
        r"([^\n，,。；;:：]{2,48}?)\s*(?:可以)?上传到([^\n，,。；;]{2,45})"
        r"[，,]\s*走([^\n，,。；;]{2,70})", text,
    ):
        source = _fallback_label(match.group(1))
        asset = _fallback_label(match.group(2))
        chain = _fallback_label(match.group(3))
        if chain.endswith("链路生成视频"):
            chain = chain[:-len("生成视频")]
        edges.extend(((source, asset), (asset, chain)))

    # A completed asset can be provided to several recipients. Pick the most
    # specific literal recipient for one safe edge; adding every recipient as
    # a graph node can make a direct edge appear to cross another node.
    provided_asset = re.search(
        r"完成([^、，,。；;\n]{2,32})(?:、[^，,。；;\n]{2,50})*"
        r"[，,]\s*提供给([^，,。；;\n]{2,60})", text,
    )
    if provided_asset:
        source = _fallback_label(provided_asset.group(1)).removesuffix("的上线")
        recipients = [_fallback_label(part).removesuffix("使用")
                      for part in re.split(r"[、及和]", provided_asset.group(2))]
        recipients = [item for item in recipients if item]
        if recipients:
            target = max(recipients, key=len)
            edges.append((source, target))

    # The core and each listed content type are literal phrases in one local
    # architecture clause. Do not connect the content types to one another.
    for match in re.finditer(
        r"以\s*([^，,。；;\n]{2,60}?)\s*为核心架构[，,]\s*上线"
        r"([^，,。；;\n]{2,90})", text,
    ):
        core = _fallback_label(match.group(1))
        targets = re.sub(r"(?:\d+|[一二三四五六七八九十]+)类内容.*$", "", match.group(2))
        for target in re.split(r"[、及和]", targets):
            label = _fallback_label(target)
            if label:
                edges.append((core, label))

    # A short heading immediately followed by its same-named plan can supply
    # several independent components. The caller joins only nearby, verified
    # spans from one source; this parser does not join distant cooccurrences.
    for match in re.finditer(
        r"(?:^|\n)\s*([^\n，,。；;:：]{2,40})\n\s*"
        r"([^\n:：]{2,24})方案[:：]\s*([^。；;]{2,180})", text,
    ):
        heading = _fallback_label(match.group(1))
        plan_kind = _fallback_label(match.group(2))
        if not heading.casefold().endswith(plan_kind.casefold()):
            continue
        for component in re.split(r"[+＋、，,\n]", match.group(3)):
            label = _fallback_label(component)
            if label:
                edges.append((heading, label))
    return edges


def _fallback_phrase_position(text: str, label: str, start: int = 0) -> tuple[int, int]:
    """Find the exact phrase while allowing source NBSP/ordinary whitespace."""
    pattern = r"\s*".join(re.escape(char) for char in label.casefold()
                           if not char.isspace())
    match = re.search(pattern, text.casefold()[start:])
    return (start + match.start(), start + match.end()) if match else (-1, -1)


def _fallback_group_status(texts: list[str], historical: bool) -> str:
    """Label a whole group only when every accepted edge has that local status."""
    base = "历史原文" if historical else "原始资料"
    if texts and all(re.search(r"KR\d+", text, re.I) for text in texts):
        quarters = {match.group(1).upper() for text in texts
                    for match in re.finditer(r"KR\d+.{0,30}?(Q[1-4])", text, re.I)}
        if len(quarters) == 1:
            return "{} · {}规划".format(base, next(iter(quarters)))
        return "{} · KR规划".format(base)
    if texts and all("完成" in text and "上线" in text for text in texts):
        return "历史已上线" if historical else "原文记载已上线"
    if texts and all("决定" in text and "推进" in text for text in texts):
        return "{} · 方案".format(base)
    if texts and all("方案" in text for text in texts):
        return "{} · 方案".format(base)
    return base


def build_evidence_diagram(references: list[dict[str, Any]]) -> str:
    """Build a conservative source-grouped fallback from verified local spans.

    This only extracts literal relations inside each source's own exact excerpt.
    The caller must still run its diagram claim audit against the original
    source scopes before committing a generated document.
    """
    groups: list[tuple[str, str, "OrderedDict[str, str]"]] = []
    edges: list[tuple[str, str]] = []
    global_edges: set[tuple[str, str]] = set()
    next_node = 1
    for reference in references[:16]:
        if not isinstance(reference, dict) or reference.get("source_type") != "document":
            continue
        snapshot_id = reference.get("source_snapshot_id")
        source_hash = reference.get("source_body_hash")
        if (type(snapshot_id) is not int or snapshot_id <= 0
                or not isinstance(source_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", source_hash)):
            continue
        source_windows = []
        for window in [reference, *(reference.get("evidence_windows") or [])]:
            if not isinstance(window, dict):
                continue
            content_bytes = str(window.get("content") or "").encode("utf-8")
            scope = window.get("source_scope") or {}
            if (not isinstance(scope, dict)
                    or scope.get("snapshot_id") != snapshot_id
                    or scope.get("source_body_hash") != source_hash
                    or scope.get("excerpt_hash") != hashlib.sha256(content_bytes).hexdigest()
                    or type(scope.get("start_byte")) is not int
                    or type(scope.get("end_byte")) is not int
                    or scope["end_byte"] - scope["start_byte"] != len(content_bytes)):
                continue
            source_windows.append((scope["start_byte"], scope["end_byte"], content_bytes))
        if not source_windows:
            continue
        buckets: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        seen_edges: set[tuple[str, str]] = set()
        verified_spans: list[tuple[str, int, int]] = []
        for raw_span in (reference.get("evidence_spans") or [])[:12]:
            if not isinstance(raw_span, dict):
                continue
            text = str(raw_span.get("text") or "")
            data = text.encode("utf-8")
            if (not text or len(text) > 1200
                    or raw_span.get("snapshot_id") != snapshot_id
                    or raw_span.get("source_body_hash") != source_hash
                    or raw_span.get("excerpt_hash") != hashlib.sha256(data).hexdigest()
                    or type(raw_span.get("start_byte")) is not int
                    or type(raw_span.get("end_byte")) is not int
                    or raw_span["end_byte"] - raw_span["start_byte"] != len(data)
                    or not any(start <= raw_span["start_byte"]
                               and raw_span["end_byte"] <= end
                               and body[raw_span["start_byte"] - start:
                                        raw_span["end_byte"] - start] == data
                               for start, end, body in source_windows)):
                continue
            verified_spans.append((text, raw_span["start_byte"], raw_span["end_byte"]))
        local_texts = [item[0] for item in verified_spans]
        ordered_spans = sorted(verified_spans, key=lambda item: item[1])
        for before, after in zip(ordered_spans, ordered_spans[1:]):
            # Prompt spans are line excerpts. A zero to two byte gap allows a
            # newline separator (including CRLF), not a distant-section join.
            if 0 <= after[1] - before[2] <= 2:
                local_texts.append(before[0] + "\n" + after[0])
        for text in local_texts:
            folded = text.casefold()
            for left, right in _fallback_span_edges(text):
                edge_key = (left.casefold(), right.casefold())
                if ((left, right) in seen_edges or edge_key in global_edges
                        or not 2 <= len(left) <= 50
                        or not 2 <= len(right) <= 50 or left == right
                        or any(term in (left + right).casefold() for term in
                               ("http", "密钥", "价格", "%", ".mp4"))):
                    continue
                left_at, left_end = _fallback_phrase_position(text, left)
                right_at, _ = _fallback_phrase_position(text, right, left_end)
                if left_at < 0 or right_at < 0 or right_at - left_at > 420:
                    continue
                between = folded[left_end:right_at]
                if not any(verb in between for verb in
                           ("沉淀", "打通", "调用", "连接", "接入", "串联",
                            "实现", "上传", "走", "提供给", "使用", "完成", "上线",
                            "核心架构", "方案", "➡", "→", "->", "—")):
                    continue
                seen_edges.add((left, right))
                global_edges.add(edge_key)
                status = _fallback_group_status(
                    [text], str(reference.get("refresh_status") or "") == "historical_only")
                if status not in buckets:
                    if len(groups) + len(buckets) >= 8:
                        continue
                    buckets[status] = {"nodes": OrderedDict(), "edges": []}
                bucket = buckets[status]
                for label in (left, right):
                    if label not in bucket["nodes"]:
                        bucket["nodes"][label] = "Node{}".format(next_node)
                        next_node += 1
                bucket["edges"].append((bucket["nodes"][left], bucket["nodes"][right]))
                if next_node > 48 or len(edges) + sum(
                        len(value["edges"]) for value in buckets.values()) >= 50:
                    break
            if next_node > 48 or len(edges) + sum(
                    len(value["edges"]) for value in buckets.values()) >= 50:
                break
        if not buckets:
            continue
        identity = "{}:{}".format(reference.get("source_type"), reference.get("source_id"))
        for status, bucket in buckets.items():
            ordinal = len(groups) + 1
            group_label = "{} · {}".format(status, identity)
            groups.append(("Source{}".format(ordinal), group_label, bucket["nodes"]))
            edges.extend(bucket["edges"])
        if len(groups) >= 8:
            break
    if not edges:
        raise ValueError("原始摘录中没有可安全提取的图示关系")
    output = ["flowchart LR"]
    for identifier, label, nodes in groups:
        output.append('    subgraph {}["{}"]'.format(identifier, label))
        output.extend('        {}["{}"]'.format(node_id, _safe_label(name))
                      for name, node_id in nodes.items())
        output.append("    end")
    output.extend("    {} --> {}".format(start, end) for start, end in edges)
    return canonicalize_standalone_diagram("```mermaid\n" + "\n".join(output) + "\n```")


def normalize_standalone_diagram(result: str) -> Tuple[str, str]:
    """Accept one renderable diagram, never an essay or an image suggestion."""
    value = str(result or "").strip()
    match = _FENCED.fullmatch(value)
    if match:
        code = match.group(1).strip()
    elif _HEADER.match(value):
        code = value
    else:
        raise ValueError("交付物不是独立 Mermaid 图示")
    lines = [line.rstrip() for line in code.splitlines() if line.strip()]
    code = "\n".join(lines)
    if len(code) > 12000 or len(lines) < 3 or not _HEADER.match(code):
        raise ValueError("图示结构不完整")
    if _UNSAFE.search(code) or _PLACEHOLDER.search(code):
        raise ValueError("图示包含不允许的指令或示例占位内容")
    if code.lower().startswith(("flowchart", "graph")):
        if not _FLOW_HEADER.fullmatch(lines[0].strip()):
            raise ValueError("flowchart 方向声明无效")
        _validate_flowchart(lines)
    elif code.lower().startswith(("sequencediagram", "statediagram")) and not _RELATION.search(code):
        raise ValueError("图示缺少实际节点关系")
    return "```mermaid\n" + code + "\n```", code


def diagram_delivery_report(document: str) -> Dict[str, Any]:
    try:
        normalized, code = normalize_standalone_diagram(document)
        passed = normalized == document.strip()
        reason = "" if passed else "存在图示之外的文字内容"
    except ValueError as exc:
        passed = False
        code = ""
        reason = str(exc)
    return {
        "status": "pass" if passed else "blocked",
        "artifact_kind": "diagram",
        "checks": [{"id": "standalone_diagram", "passed": passed,
                    "evidence": code.splitlines()[0] if passed else "", "reason": reason}],
        "corrections": [] if passed else [reason],
    }
