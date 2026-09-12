"""Readable comparisons derived only from validated material field bindings."""
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Tuple

from .material_evidence import _quantity, _unit_identity


_DURATION_UNITS = frozenset(("s", "ms", "us", "µs", "μs", "ns", "sec", "seconds", "秒", "毫秒", "微秒", "纳秒"))
_AVERAGE = re.compile(r"\b(?:avg|mean|average)\b|平均", re.IGNORECASE)
_TAIL = re.compile(r"\bP(?:95|99)\b", re.IGNORECASE)
_MAX_CHART_NOTES = 4


@dataclass
class _Reading:
    name: str
    amount: Decimal
    unit: str
    display: str
    before: str = ""
    direction: str = ""


@dataclass
class _Row:
    group: Tuple[str, ...]
    name: str
    readings: List[_Reading] = field(default_factory=list)


def _rows(bindings: str) -> List[_Row]:
    rows = []
    current = None
    for line in (bindings or "").splitlines():
        header = re.fullmatch(r"-\s+(.+)[：:]\s*", line)
        if header:
            parts = [part.strip() for part in header.group(1).split(" / ")]
            current = _Row(tuple(parts[:-1]), parts[-1])
            rows.append(current)
            continue
        item = re.fullmatch(r"\s+-\s+(.+?) = (.+)", line)
        if not current or not item:
            continue
        name, full_value = item.groups()
        raw_value, _, comparison = full_value.partition("；相对基线：")
        quantity = _quantity(name, raw_value)
        if quantity is None:
            continue
        reading = _Reading(name, quantity[0], quantity[1], quantity[2])
        change = re.fullmatch(r"从(.+?)(升至|降至)(.+)", comparison)
        if change:
            old, new = _quantity(name, change.group(1)), _quantity(name, change.group(3))
            if (old is not None and new is not None and new[0] == quantity[0]
                    and _unit_identity(old[1]) == _unit_identity(new[1]) == _unit_identity(quantity[1])):
                difference = new[0] - old[0]
                if (difference > 0 and change.group(2) == "升至") or (difference < 0 and change.group(2) == "降至"):
                    reading.before = old[2]
                    reading.direction = "上升" if difference > 0 else "下降"
        current.readings.append(reading)
    return rows


def _duration(reading: _Reading) -> bool:
    return reading.unit.lower() in _DURATION_UNITS


def _lowest_duration_row(rows: List[_Row]) -> str:
    if len(rows) < 2 or len({row.name for row in rows}) != len(rows):
        return ""
    measurements = []
    for row in rows:
        values = [reading for reading in row.readings if _duration(reading)]
        fields = {reading.name: reading for reading in values}
        if not fields or len(fields) != len(values):
            return ""
        measurements.append(fields)
    names = set(measurements[0])
    if any(set(values) != names for values in measurements):
        return ""
    if any(len({_unit_identity(values[name].unit) for values in measurements}) != 1 for name in names):
        return ""
    for index, values in enumerate(measurements):
        if all(values[name].amount < other[name].amount for other_index, other in enumerate(measurements)
               if other_index != index for name in names):
            return rows[index].name
    return ""


def _chart_summaries(analysis_input: str) -> List[str]:
    summaries = []
    markers = list(re.finditer(r"(?m)^【([^】\n]+)】[ \t]*\r?$", analysis_input or ""))
    for index, marker in enumerate(markers):
        if marker.group(1).endswith("结束"):
            continue
        end = markers[index + 1].start() if index + 1 < len(markers) else len(analysis_input)
        body = analysis_input[marker.end():end]
        if "图表可确认文字与口径：" not in body:
            continue
        notes = body.split("图表可确认文字与口径：", 1)[1].split("\n本图", 1)[0]
        notes = list(dict.fromkeys(line.strip() for line in notes.splitlines() if line.strip()))[:_MAX_CHART_NOTES]
        text = "可确认的文字包括：" + "；".join(notes) + "。" if notes else ""
        summaries.append("**%s**\n\n%s当前可识别文字不足以确认曲线数值、走势、各系列状态，以及与其他图片的对应关系。" % (marker.group(1), text))
    return summaries


def build_material_comparison_summary(bindings: str, analysis_input: str) -> str:
    """Summarize explicit baseline comparisons without model arithmetic."""
    groups = OrderedDict()
    for row in _rows(bindings):
        groups.setdefault(row.group, []).append(row)
    sections = []
    for group, rows in groups.items():
        facts = []
        for row in rows:
            eligible = [reading for reading in row.readings if reading.direction and (
                (_duration(reading) and (_AVERAGE.search(reading.name) or _TAIL.search(reading.name)))
                or reading.unit == "%")]
            eligible.sort(key=lambda reading: (
                0 if _duration(reading) and _AVERAGE.search(reading.name) else
                1 if _duration(reading) and reading.direction == "上升" else
                2 if _duration(reading) else 3))
            if eligible:
                details = "；".join("%s%s：%s→%s" % (r.name, r.direction, r.before, r.display) for r in eligible)
                facts.append("- %s：%s。" % (row.name, details))
        if not facts:
            continue
        lowest = _lowest_duration_row(rows)
        if lowest:
            facts.insert(0, "- %s：这些可比较的耗时指标均低于表中其他对象。" % lowest)
        label = " · ".join(group) or "表格对比"
        sections.append("**%s**\n\n%s" % (label, "\n".join(facts)))
    if not sections:
        return ""
    explanation = "以下箭头表示材料中的基线读数→对应对象读数。"
    coverage = "以上比较仅覆盖材料中能够明确确认的字段。"
    return "\n\n".join([explanation] + sections + _chart_summaries(analysis_input) + [coverage])
