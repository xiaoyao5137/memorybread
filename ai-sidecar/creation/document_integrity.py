"""Deterministic acceptance checks shared by local and external creation nodes."""
import re
from collections import Counter
from typing import Any, Dict, List, Tuple

# 候选稿最后一章如果明显短于基线同名章节，按输出截断处理。
MIN_TAIL_SECTION_RATIO = 0.6


def _normalize_title(title: str) -> str:
    return re.sub(r"[*_`\s]", "", title)


def _heading_entries(document: str) -> List[Tuple[int, int, str]]:
    """标题索引 (起始偏移, 层级, 归一标题)；围栏代码块内的 # 不作为标题。"""
    entries: List[Tuple[int, int, str]] = []
    offset = 0
    fence = ""
    for line in document.splitlines(keepends=True):
        bare = line.rstrip("\n")
        marker = re.match(r"^\s*(```+|~~~+)", bare)
        if marker:
            token = marker.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ""
            offset += len(line)
            continue
        heading = None if fence else re.match(r"^(#{1,6})\s+(.+?)\s*$", bare)
        if heading:
            entries.append((offset, len(heading.group(1)), _normalize_title(heading.group(2))))
        offset += len(line)
    return entries


def level2_sections(document: str) -> List[Dict[str, Any]]:
    """二级章节区间（含本标题行，直到下一个不更深层的标题）。"""
    entries = _heading_entries(document)
    sections: List[Dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if entry[1] != 2:
            continue
        stop = len(document)
        for following in entries[index + 1:]:
            if following[1] <= 2:
                stop = following[0]
                break
        sections.append({"title": entry[2], "start": entry[0], "end": stop})
    return sections


def merge_rewritten_sections(base: str, candidate: str) -> str:
    """把被截断的重写稿按二级章节合并回基线，只采纳能安全接管的部分。

    自动修正常见失败模式：候选稿前几章已是完整新版，后面章节因输出长度停笔
    而直接丢失。旧行为把整份候选稿丢弃，正文一字未改，下一轮验收必然得出相同
    结论，修正预算全部空转在无效重试上。这里按章节取用：候选稿覆盖到的沿用新版，
    缺失的沿用基线原文，保证已真正改写的部分能落地。

    只按基线章节顺序合并，不新增候选稿章节，避免把残段追加到正文尾部。
    返回 "" 表示没有任何章节可采纳，调用方应保持原有拒绝行为。
    """
    if not base.strip() or not candidate.strip():
        return ""
    base_sections = level2_sections(base)
    candidate_sections = level2_sections(candidate)
    if not base_sections or not candidate_sections:
        return ""
    by_title: Dict[str, Dict[str, Any]] = {}
    for item in candidate_sections:
        by_title.setdefault(str(item["title"]), item)
    tail_start = max(int(item["start"]) for item in candidate_sections)
    adopted = 0
    pieces: List[str] = []
    cursor = 0
    for section in base_sections:
        original = base[section["start"]:section["end"]]
        pieces.append(base[cursor:section["start"]])
        cursor = section["end"]
        incoming = by_title.get(str(section["title"]))
        if incoming is None:
            pieces.append(original)
            continue
        fragment = candidate[int(incoming["start"]):int(incoming["end"])]
        if (
            int(incoming["start"]) == tail_start
            and len(fragment.strip()) < len(original.strip()) * MIN_TAIL_SECTION_RATIO
        ):
            # 候选稿最后一章且明显偏短：按截断残段处理，沿用基线完整章节。
            pieces.append(original)
            continue
        if not fragment.strip():
            pieces.append(original)
            continue
        pieces.append(fragment)
        adopted += 1
    if not adopted:
        return ""
    pieces.append(base[cursor:])
    merged = "".join(pieces).strip()
    # 拼接可能让上一章残行紧贴标题，统一补回标题前的空行，不改动任何正文文字。
    return re.sub(r"([^\n])\n+(#{2}\s)", r"\1\n\n\2", merged)


def integrity_problems(document: str, base: str = "", preserve_sections: bool = False,
                       allow_structure_change: bool = False) -> List[str]:
    problems = []
    if not document.strip():
        return ["empty_document"]
    # Ignore code, table separators and short repeated labels; detect substantive
    # prose loops even when the model varies list numbering or whitespace.
    prose = re.sub(r"```.*?(?:```|$)", "", document, flags=re.S)
    prose = re.sub(r"<!--.*?-->", "", prose, flags=re.S)
    prose = re.sub(r"^\s*\|.*\|\s*$", "", prose, flags=re.M)
    units = []
    for line in prose.splitlines():
        line = re.sub(r"^[\s>*#\-\d.)、]+", "", line)
        line = re.sub(r"[\s*_`]+", "", line)
        if len(line) >= 40:
            units.append(line)
    counts = Counter(units)
    repeated = sum(len(unit) * (count - 1) for unit, count in counts.items() if count >= 3)
    if any(count >= 4 for count in counts.values()) and repeated >= 240:
        problems.append("repeated_content")
    # Also catches a single paragraph repeating sentences without line breaks.
    sentences = Counter(s.strip() for s in re.split(r"[。！？!?\n]", prose) if len(s.strip()) >= 35)
    if any(count >= 5 for count in sentences.values()) and "repeated_content" not in problems:
        problems.append("repeated_content")
    # A heading marker glued to prose/list content is rendered as plain text,
    # even though a semantic reviewer may mistake it for a real section.
    if re.search(r"(?m)(?<=[^\n\\#])#{2,6}[ \t]+\S", prose):
        problems.append("malformed_heading_boundary")
    if preserve_sections and base.strip():
        def headings(text):
            return [re.sub(r"[*_`\s]", "", h) for h in re.findall(r"^##\s+(.+)$", text, re.M)]
        before, after = headings(base), headings(document)
        # 交付修复（delivery_repair）可按核验意见重排章节结构；此时只跳过
        # “二级标题逐字不变”这一条，重复正文与正文丢失守卫仍保留。
        # 普通润色（allow_structure_change=False）仍禁止改动二级章节结构。
        if before and before != after and not allow_structure_change:
            problems.append("section_structure_changed")
        if len(base) >= 500 and len(document) < len(base) * 0.55:
            problems.append("document_content_lost")
    return problems


RISK_WRITING_POLICY = """数据引用与风险说明：只采用直接支持当前任务的来源事实。qualified 不表示必须补入正文；采用时标明参考值和真实统计周期，不冒充目标周期实绩。仅披露实际采用的数据的具体限制，同一来源、周期和风险合并说明一次，不逐句重复【风险标注】。未指定目标周期时不得自行引入本周实绩或环比限制。结构化 data_risks 的说明由系统统一生成，正文不重复输出风险说明块。普通历史背景写明时间即可，不扩大为整篇文档的风险提示。润色保留事实、来源与必要限定，但应合并重复说明，不新增无关数据。"""


def require_complete_generation(event):
    """Transport completion is not document completion when the token budget ends."""
    from .operations import OperationError
    reasons = [event.get("done_reason"), (event.get("delta") or {}).get("stop_reason")]
    reasons.extend(choice.get("finish_reason") for choice in event.get("choices", []) if isinstance(choice, dict))
    if any(reason in {"length", "max_tokens"} for reason in reasons):
        raise OperationError("CREATION_DOCUMENT_TRUNCATED", "模型输出达到长度上限，未提交不完整正文，请缩小本次生成范围后重试")
