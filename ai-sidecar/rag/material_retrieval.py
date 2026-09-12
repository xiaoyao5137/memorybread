"""Bind material-dependent consultation questions to their supplied subject.

The envelope is a string for existing prompt/history consumers, but retrieval
metadata is carried as attributes, never parsed from user or OCR text.
"""
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Tuple


_MATERIAL_REFERENCE = re.compile(
    r"(?:这|那|上述|上面|下面|附件|所附|当前|本次)(?:[一二两三四五六七八九十\d]*"
    r"(?:个|份|张|幅|段|组|篇|条|些|种|次)?)?\s*(?:[A-Za-z][A-Za-z0-9._+-]*\s*)?"
    r"(?:报告|图|表|材料|文件|内容|数据|结果|截图|文档|记录|指标|曲线)"
    r"|(?:附上|附带|提供|上传)(?:的)?(?:图|文件|报告|材料)"
    r"|@(?:图片|图像|文件|附件)\s*\d+"
    r"|(?:他|她|他们|她们|对方)(?:在|想|要)?(?:表达|说|讲|想表达)"
    r"|(?:这|那)(?:个|些)?(?:是|说明|意味|体现|代表)"
    r"|\b(?:this|these|that|those|attached)\s+(?:report|chart|table|image|document|result)s?\b",
    re.IGNORECASE,
)
_INSTRUCTION_LINE = re.compile(
    r"忽略.{0,12}(?:指令|规则)|(?:不要|无需|禁止|只需|必须|请)(?:再)?(?:检索|召回|搜索|回答|输出|执行)"
    r"|(?:检索问题|核心问题|检索材料主题|咨询检索上下文)\s*[:：]"
    r"|\b(?:ignore|disregard).{0,30}(?:instruction|rule)|\b(?:do not|don't)\s+(?:retrieve|search)"
    r"|\b(?:system|assistant)\s*:", re.IGNORECASE,
)
_GENERIC = frozenset((
    "报告", "结论", "体现", "结果", "内容", "分析", "数据", "图表", "测试", "模型",
    "性能", "对比", "比较", "指标", "时间", "说明", "总结", "当前", "本次", "截图",
    "report", "table", "chart", "figure", "model", "results", "result", "test", "total",
    "the", "and", "for", "with", "from", "this", "that", "these", "those", "are", "was",
    "batch", "size", "mean", "median", "min", "max", "avg", "output", "input", "latency",
    "tokens", "token", "throughput", "time", "date", "http", "https", "png", "jpg", "jpeg", "pdf",
    "base", "baseline", "pp", "ms", "us", "ns", "sec", "seconds", "percent", "qps", "rps",
    "e2e", "ttft", "tpot", "gpu", "gpus", "cpu", "cpus", "cache",
    "任务名称", "模型名称", "每实例", "副本数", "成功率", "调用数", "请求数",
    "项目名称", "平均延迟", "峰值吞吐", "平均耗时", "最大耗时", "最小耗时",
    "success", "rate", "request", "requests", "instance", "instances", "count", "name",
))
_ASCII_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[._+-][A-Za-z0-9]+)*")
_AXIS_OR_CONFIGURATION = re.compile(r"(?:p\d{1,3}(?:\.\d+)?|(?:tp|dp|pp|ep|cp|sp)\d+)$")
_MEASUREMENT_LITERAL = r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?"
_MEASUREMENT_TOKEN = re.compile(r"(?<![A-Za-z0-9_.])(" + _MEASUREMENT_LITERAL + r")(?![\d.])")


def _normalize_measurement(value: str):
    try:
        number = Decimal(value.replace(",", ""))
        if not number.is_finite() or abs(number.adjusted()) > 64 or number == number.to_integral_value():
            return None
        return format(number, "f").rstrip("0").rstrip(".")
    except (InvalidOperation, ValueError):
        return None


def extract_material_measurement_anchors(bindings: str) -> Tuple[str, ...]:
    """Read only original field values from controlled field-binding output.

    Integer configurations, comparison annotations and baseline commentary are
    not additional measurements. Decimal spellings share one normalized key.
    """
    anchors = []
    for line in (bindings or "").splitlines():
        _, separator, value = line.partition(" = ")
        if not separator:
            continue
        match = re.match(r"\s*(" + _MEASUREMENT_LITERAL + r")(?![\d.])", value)
        if match:
            normalized = _normalize_measurement(match.group(1))
            if normalized and normalized not in anchors:
                anchors.append(normalized)
    return tuple(anchors)


def count_material_measurement_matches(text: str, anchors: Tuple[str, ...]) -> int:
    """Count distinct original decimal readings, never numeric substrings."""
    text = re.sub(r"[（(][^()（）]*(?:%|％|pp|百分点)[^()（）]*[）)]", "", text or "", flags=re.IGNORECASE)
    expected = {_normalize_measurement(str(anchor)) for anchor in anchors}
    observed = {_normalize_measurement(match.group(1)) for match in _MEASUREMENT_TOKEN.finditer(text)}
    return len((expected - {None}) & (observed - {None}))


def needs_material_subject(instruction: str) -> bool:
    return bool(_MATERIAL_REFERENCE.search(instruction or ""))


def requests_material_history(instruction: str) -> bool:
    """Only positive requests in the user's instruction relax material scope.

    Treat negation within its clause, so 'ignore history; compare the images'
    does not become a request for history merely by mentioning the word.
    """
    historical = re.compile(
        r"历史|以前|之前|以往|此前|上次|上周|上月|过去"
        r"|结合.{0,12}(?:我的|工作|记录|资料)"
        r"|\b(?:history|historical|previous|previously|past)\b", re.IGNORECASE)
    negative = re.compile(
        r"不要|不用|无需|不必|不参考|不结合|别|忽略|排除"
        r"|\b(?:ignore|exclude|without|avoid|disregard|do not|don't|no)\b",
        re.IGNORECASE)
    for clause in re.split(r"[，,。.!！?？;；\n]", instruction or ""):
        for match in historical.finditer(clause):
            if not negative.search(clause[:match.start()]):
                return True
    return False


def _line_anchors(line: str):
    terms = []
    for token in _ASCII_TOKEN.findall(line):
        for component in re.split(r"[._+-]", token):
            lowered = component.lower()
            if (len(lowered) >= 2 and not lowered.isdigit() and lowered not in _GENERIC
                    and not _AXIS_OR_CONFIGURATION.fullmatch(lowered)):
                terms.append(lowered)
    for caption in re.findall(r"[\u4e00-\u9fff]+", line):
        caption = caption.strip("的了是在与和及或中里这那")
        if 3 <= len(caption) <= 18 and caption not in _GENERIC:
            terms.append(caption)
    return terms


def extract_material_anchors(material: str, limit: int = 16) -> Tuple[str, ...]:
    """Use identifiers and compact captions, not arbitrary prompt instructions.

    No domain vocabulary or model-specific identifiers are required. Numeric
    table cells, dates, generic labels and control-like lines are not subjects.
    Chinese captions remain whole phrases instead of character n-grams.
    """
    occurrences = []
    table_objects = []
    column_subjects = []
    for line in (material or "")[:16000].splitlines():
        if re.fullmatch(r"\s*(?:【[^】]+】|\[[^\]]+\])\s*", line):
            column_subjects = []
            continue
        if _INSTRUCTION_LINE.search(line):
            column_subjects = []
            continue
        line = re.sub(r"https?://\S+", " ", line)
        cells = [cell.strip() for cell in re.split(r"\||\t", line.strip("|"))]
        if len(cells) > 1 and all(re.fullmatch(r"[:\-\s]+", cell) for cell in cells):
            # Markdown's alignment row belongs to the preceding header.
            continue
        # Data rows carry numeric measurements. Their first nonnumeric entity
        # cell identifies the object, including a two-column name/value table.
        # Transposed comparisons instead name objects in the preceding header.
        cell_names = [_line_anchors(cell) for cell in cells]
        # Digits inside identifiers/percentile labels (P95, E2E) are not
        # measurements; otherwise a header could be mistaken for a data row.
        numeric_cells = [bool(re.search(r"(?<![A-Za-z0-9])[-+]?\d", cell)) and not names
                         for cell, names in zip(cells, cell_names)]
        if len(cells) >= 2 and any(numeric_cells):
            row_subject = next((names for names, numeric in zip(cell_names, numeric_cells)
                                if names and not numeric), [])
            if row_subject:
                table_objects.extend(row_subject)
            elif len(column_subjects) == len(cells):
                for names, numeric in zip(column_subjects, numeric_cells):
                    if numeric:
                        table_objects.extend(names)
        elif len(cells) >= 2:
            column_subjects = cell_names
        elif len(cells) == 1:
            column_subjects = []
            occurrences.extend(_line_anchors(line))
    # A comparative table names its subjects explicitly. Prefer those names
    # over incidental captions such as percentiles, hardware and success rates.
    occurrences = table_objects or occurrences
    counts = Counter(occurrences)
    # Repeated experiment/object names outrank incidental prose. Keep original
    # order within each frequency, so title/table headings retain their priority.
    unique = list(dict.fromkeys(occurrences))
    unique.sort(key=lambda term: -counts[term])
    return tuple(unique[:limit])


class ConsultationQuery(str):
    """Internal, non-textual retrieval metadata attached to a generated prompt."""

    def __new__(cls, prompt: str, instruction: str, retrieval_query: str,
                material_anchors: Tuple[str, ...] = (),
                requires_material_grounding: bool = False,
                material_min_anchor_matches: int = 1,
                material_measurement_anchors: Tuple[str, ...] = (),
                material_min_measurement_matches: int = 2):
        value = str.__new__(cls, prompt)
        value.instruction = instruction
        value.retrieval_query = retrieval_query
        value.material_anchors = material_anchors
        value.requires_material_grounding = requires_material_grounding
        value.material_min_anchor_matches = max(1, material_min_anchor_matches)
        value.material_measurement_anchors = material_measurement_anchors
        value.material_min_measurement_matches = max(1, material_min_measurement_matches)
        return value


def material_query_parts(instruction: str, retrieval_query: str,
                         material: str) -> Tuple[str, Tuple[str, ...], bool]:
    required = needs_material_subject(instruction)
    anchors = ()
    if required:
        explicit = [term.lower() for term in _ASCII_TOKEN.findall(instruction or "")
                    if len(term) >= 2 and term.lower() not in _GENERIC]
        anchors = tuple(dict.fromkeys([*explicit, *extract_material_anchors(material)]))[:16]
    original = " ".join((instruction or "").split())
    retrieval = " ".join((retrieval_query or original).split())
    if required:
        # Preserve the original question, including any explicit subject; the
        # material only resolves its referent and cannot supply retrieval rules.
        retrieval = original
        if anchors:
            retrieval += "\n材料主题：" + " ".join(anchors)
    return retrieval, anchors, required
