"""Project OCR into evidence that an answer model can safely interpret.

This is generation input only. It does not edit original OCR or images.
Unbound chart ticks never become measured statistics.
"""
import re

from .material_evidence import build_material_value_bindings, _compound_fields


_PERCENTILE = re.compile(r"(?<![A-Za-z0-9])P\d{1,3}(?![A-Za-z0-9])", re.IGNORECASE)
_NUMBER = re.compile(r"[+−-]?\d+(?:[.,:]\d+)*")
_PERIOD = re.compile(r"(?:最近|过去|近|last|past)\s*\d+\s*(?:秒|分钟|小时|天|seconds?|minutes?|hours?|days?)", re.IGNORECASE)
_UNIT = re.compile(r"(?:ns|us|ms|sec|seconds?|mins?|minutes?|hours?|hz|s|m|h|d|µs|μs)", re.IGNORECASE)


def _tick_line(line: str) -> bool:
    if not re.search(r"\d", line):
        return False
    rest = _NUMBER.sub("", line)
    rest = _UNIT.sub("", rest)
    return not re.sub(r"[\s|.,:;/%()（）+−\-–—]", "", rest)


def _legend_line(line: str) -> bool:
    if not _PERCENTILE.search(line):
        return False
    rest = _PERCENTILE.sub("", line)
    rest = re.sub(r"\b(?:avg|average|mean|median|min|max)\b|平均|最大|最小|中位数", "", rest, flags=re.IGNORECASE)
    return not re.sub(r"[\s|.,:/_+−\-–—]", "", rest)


def _strip_change_annotations(bindings: str) -> str:
    output = []
    for line in bindings.splitlines():
        left, separator, right = line.partition(" = ")
        if separator:
            value, marker, comparison = right.partition("；相对基线")
            value = re.sub(r"\s*[（(][^()（）]*(?:%|％|pp|百分点)[^()（）]*[）)]", "", value, flags=re.IGNORECASE)
            line = left + separator + value.strip() + marker + comparison
        output.append(line)
    return "\n".join(output)


def _non_table_text(body: str) -> str:
    output = []
    in_table = False
    for line in body.splitlines():
        if "|" in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if any(_compound_fields(cell) for cell in cells):
                in_table = True
                continue
            # A prose explanation can contain a literal vertical bar.
            prose = bool(re.search(r"(?:说明|备注|注意|原因|结论)[:：]|\b(?:note|explanation)\s*:", line, re.IGNORECASE))
            if not prose and (in_table or _tick_line(line) or _legend_line(line)):
                continue
        elif line.strip():
            in_table = False
        if not _tick_line(line) and not _legend_line(line):
            output.append(line)
    return "\n".join(output).strip()


def _chart_notes(body: str) -> str:
    notes = []
    for line in body.splitlines():
        if _tick_line(line) or _legend_line(line):
            continue
        for cell in line.split("|"):
            cell = cell.strip()
            if not cell or _PERCENTILE.search(cell) or _UNIT.fullmatch(cell):
                continue
            if re.search(r"\d", cell):
                # Retain explicit time scope, but never an unnamed number.
                periods = list(_PERIOD.finditer(cell))
                if periods and not re.search(r"\d", _PERIOD.sub("", cell)):
                    notes.append(cell)
                else:
                    notes.extend(match.group(0) for match in periods)
                continue
            if re.search(r"[A-Za-z\u4e00-\u9fff]", cell) and cell not in {"查看读数", "时间范围", "完整", "V"}:
                notes.append(cell)
    return "\n".join(dict.fromkeys(notes))


def _project(body: str, label: str = "") -> str:
    wrapped = "【%s】\n%s\n【%s结束】" % (label, body, label) if label else body
    bindings = build_material_value_bindings(wrapped)
    if bindings:
        prose = _non_table_text(body)
        result = ("非表格文字说明：\n" + prose + "\n\n") if prose else ""
        result += "材料字段逐项对照（原值及与明确基线的升降方向）：\n" + _strip_change_annotations(bindings)
        result += "\n未列出的字段或读数未能确定，不得补写；数值升降不等于业务优劣。"
        return "\n" + result + "\n"
    lines = body.splitlines()
    chart = (len(set(value.lower() for value in _PERCENTILE.findall(body))) >= 2
             and any(_legend_line(line) for line in lines)
             and sum(_tick_line(line) for line in lines) >= 2)
    if chart:
        notes = _chart_notes(body)
        return ("\n图表可确认文字与口径：\n" + notes
                + "\n本图尚未确认各系列的具体数值、走势或状态，也未确认与其他图片的对应关系。"
                "不得把坐标刻度、图例名称或它们的位置当作测量结果。\n")
    return body


def build_material_analysis_input(evidence: str) -> str:
    """Process each marked image independently; preserve ordinary OCR text."""
    markers = list(re.finditer(r"(?m)^【([^】\n]+)】[ \t]*\r?$", evidence or ""))
    starts = [index for index, marker in enumerate(markers) if not marker.group(1).endswith("结束")]
    if not starts:
        return _project(evidence or "")
    output = []
    cursor = 0
    for index in starts:
        marker = markers[index]
        body_start = marker.end()
        body_end = markers[index + 1].start() if index + 1 < len(markers) else len(evidence)
        output.append(evidence[cursor:body_start])
        output.append(_project(evidence[body_start:body_end], marker.group(1)))
        cursor = body_end
    output.append(evidence[cursor:])
    return "".join(output)
