"""Deterministic field/value bindings for OCR comparison tables (Python 3.9).

Only table headers and explicit row cells are transcribed. Chart ticks and
legends are not observations. No values, deltas or missing columns are inferred.
"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple


_STAT = r"(?:average|avg|mean|median|min|max|std|p\d{1,3}(?:\.\d+)?|q[1-4]|平均|中位数|最小|最大)"
_NUMBER = re.compile(r"^[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?(?:\s*(?:%|[A-Za-zµμ°]+))?$")
_VALUE_PARTS = re.compile(r"^([+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?)(?:\s*(%|[A-Za-zµμ°]+))?$")


def _parts(cell: str) -> List[str]:
    """Split explicit slash-separated reading cells."""
    return [part.strip() for part in cell.split("/")]


def _reading(value: str) -> bool:
    normalized = value.replace("（", "(").replace("）", ")").replace("−", "-")
    outside = re.sub(r"\([^()]*\)", "", normalized).strip()
    return bool(_NUMBER.fullmatch(outside))


def _certain_reading(value: str) -> str:
    if _reading(value):
        return value
    # Recover only an explicitly complete annotated reading followed by one
    # separate complete reading. Never attach the second measurement to this
    # field or recover a number from arbitrary trailing prose.
    match = re.fullmatch(r"(.+?[（(][^()（）]*[）)])\s*(.+)", value)
    if match and _reading(match.group(1)) and _reading(match.group(2)):
        return match.group(1).strip()
    return ""


def _identity(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _unit_identity(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _quantity(field: str, value: str):
    normalized = value.replace("（", "(").replace("）", ")").replace("−", "-")
    outside = re.sub(r"\([^()]*\)", "", normalized).strip()
    match = _VALUE_PARTS.fullmatch(outside)
    if not match:
        return None
    declared = re.search(r"[（(]([^()（）]+)[）)]$", field)
    header_unit = declared.group(1).strip() if declared else ""
    unit = match.group(2) or header_unit
    if header_unit and match.group(2) and _unit_identity(unit) != _unit_identity(header_unit):
        return None
    try:
        number = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    return number, unit, match.group(1) + unit


def _baseline_comparison(field: str, value: str, baseline: str) -> str:
    current, previous = _quantity(field, value), _quantity(field, baseline)
    if current is None or previous is None or _unit_identity(current[1]) != _unit_identity(previous[1]):
        return ""
    delta = current[0] - previous[0]
    if not delta:
        return "；相对基线：与基线相同（%s）" % current[2]
    # The displayed cells may already be rounded. Recomputing percentages can
    # conflict with the source annotation, so only establish numeric direction.
    direction = "升至" if delta > 0 else "降至"
    return "；相对基线：从%s%s%s" % (previous[2], direction, current[2])


def _compound_fields(header: str) -> Optional[List[str]]:
    header = header.strip()
    unit = ""
    match = re.search(r"\s*[（(]([^()（）]+)[）)]\s*$", header)
    if match:
        unit = match.group(1).strip()
        header = header[:match.start()].strip()
    parts = _parts(header)
    if len(parts) < 2:
        return None
    first = re.fullmatch(r"(.*?)\s*(" + _STAT + r")", parts[0], re.IGNORECASE)
    if not first or not first.group(1).strip(" :："):
        return None
    prefix = first.group(1).strip(" :：")
    statistics = [first.group(2)]
    for part in parts[1:]:
        part = re.sub(r"^" + re.escape(prefix) + r"\s*", "", part, flags=re.IGNORECASE)
        if not re.fullmatch(_STAT, part, re.IGNORECASE):
            return None
        statistics.append(part)
    suffix = "（%s）" % unit if unit else ""
    return ["%s %s%s" % (prefix, statistic, suffix) for statistic in statistics]


def _alignment_score(expected: List[int], observed: List[int]) -> int:
    previous = [0] * (len(observed) + 1)
    for count in expected:
        current = [0]
        for index, actual in enumerate(observed):
            current.append(max(previous[index + 1], current[-1],
                               previous[index] + (1 if count == actual else 0)))
        previous = current
    return previous[-1]


def _certain_matches(expected: List[int], observed: List[int]) -> List[Tuple[int, int]]:
    """Keep only pairs present in every maximum ordered count alignment."""
    best = _alignment_score(expected, observed)
    matches = []
    for i, count in enumerate(expected):
        # If an optimal alignment can omit this header, its assignment is not
        # certain (e.g. one 4-value cell beneath two 4-field metric headers).
        if _alignment_score(expected[:i] + expected[i + 1:], observed) == best:
            continue
        positions = [j for j, actual in enumerate(observed)
                     if count == actual
                     and _alignment_score(expected[:i], observed[:j]) + 1
                     + _alignment_score(expected[i + 1:], observed[j + 1:]) == best]
        if len(positions) == 1:
            matches.append((i, positions[0]))
    return matches


def build_material_value_bindings(evidence: str) -> str:
    """Return auditable, uniquely aligned table readings; otherwise return ''."""
    records = []
    image_label = ""
    section = ""
    image_baseline = ""
    section_baselines = {}
    table_id = 0
    headers = []
    compounds = []
    for raw in (evidence or "")[:60000].splitlines():
        raw = raw.strip()
        wrapper = re.fullmatch(r"【([^】]+)】", raw)
        if wrapper:
            headers, compounds = [], []
            if not wrapper.group(1).endswith("结束"):
                image_label = wrapper.group(1)
                section = ""
                image_baseline = ""
                section_baselines = {}
            continue
        if "|" not in raw:
            baseline = re.match(r"^(?:相对\s*|relative\s+to\s+)(.+?)\s*[:：]", raw, re.IGNORECASE)
            if baseline:
                if section:
                    section_baselines[section] = baseline.group(1).strip()
                else:
                    image_baseline = baseline.group(1).strip()
            if raw and len(raw) <= 48 and not re.search(r"\d", raw) and re.search(
                    r"(?:层|阶段|部分|章节|layer|level|section)\s*[:：]?$", raw, re.IGNORECASE):
                section = raw.rstrip(" :：")
                headers, compounds = [], []
            continue
        cells = [cell.strip() for cell in raw.strip("|").split("|")]
        if len(cells) > 32 or len(cells) < 2:
            continue
        detected = [(i, fields) for i, cell in enumerate(cells)
                    for fields in [_compound_fields(cell)] if fields]
        if detected:
            headers, compounds = cells, detected
            table_id += 1
            continue
        if not compounds or not cells[0] or _reading(cells[0]):
            continue
        if all(re.fullmatch(r"[:\-\s]+", cell) for cell in cells):
            continue
        if not any(_certain_reading(part) for cell in cells[1:] for part in _parts(cell)):
            # A new, unsupported header (or an entirely unreadable row) cannot
            # establish continuation of the preceding table. Stop binding until
            # another explicit composite header is recognized, rather than
            # assigning later values to stale metrics and their old baseline.
            headers, compounds = [], []
            continue
        candidates = []
        for index, cell in enumerate(cells[1:], 1):
            values = _parts(cell)
            # A merged trailing value can invalidate one segment without
            # invalidating the other explicitly separated readings.
            if len(values) >= 2 and sum(bool(_certain_reading(value)) for value in values) >= len(values) - 1:
                candidates.append((index, values))
        matched = _certain_matches([len(fields) for _, fields in compounds],
                                   [len(values) for _, values in candidates])
        bound = []
        matched_columns = set()
        for group_index, candidate_index in matched:
            header_index, fields = compounds[group_index]
            cell_index, values = candidates[candidate_index]
            matched_columns.add(header_index)
            for field, value in zip(fields, values):
                reading = _certain_reading(value)
                if reading:
                    bound.append((field, reading))
        if not bound:
            continue
        # Ordinary columns are positional only when all columns are present and
        # every composite group confirms its original position. A missing GPU
        # cell must never shift a success-rate or request-count assignment.
        aligned = len(headers) == len(cells) and len(matched) == len(compounds) and all(
            compounds[i][0] == candidates[j][0] for i, j in matched)
        if aligned:
            for index, (header, cell) in enumerate(zip(headers[1:], cells[1:]), 1):
                if index not in matched_columns and _reading(cell):
                    bound.append((header, cell))
        records.append((image_label, section, table_id, cells[0], bound,
                        section_baselines.get(section, image_baseline)))
    lines = []
    for image, layer, table, row, bound, baseline_name in records:
        scope = " / ".join(part for part in (image, layer, row) if part)
        lines.append("- %s：" % scope)
        baseline_rows = [values for other_image, other_layer, other_table, other_row, values, _ in records
                         if baseline_name and (other_image, other_layer, other_table) == (image, layer, table)
                         and _identity(other_row) == _identity(baseline_name)]
        # Duplicate baseline rows are ambiguous, even if their names match.
        baseline_values = dict(baseline_rows[0]) if len(baseline_rows) == 1 else {}
        for field, value in bound:
            comparison = ""
            if _identity(row) != _identity(baseline_name) and field in baseline_values:
                comparison = _baseline_comparison(field, value, baseline_values[field])
            lines.append("  - %s = %s%s" % (field, value, comparison))
    return "\n".join(lines)
