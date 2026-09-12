"""Reject numeric claims invented by a successful coverage explanation.

This checks the explanation against the candidate, not whether every number in
the user's input must be used. Semantic coverage remains the reviewer's task.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .operations import OperationError


_DIGITS = dict(zip("零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖", "001223456789123456789"))
_SMALL_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_LARGE_UNITS = {"万": 10000, "萬": 10000, "亿": 100000000, "億": 100000000}
_CN_CHARS = "零〇一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億点"
_ARABIC = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
_NUMBER = re.compile(
    r"(?P<prefix>百分之|千分之|万分之)?(?P<number>" + _ARABIC + "|[" + _CN_CHARS + r"]+)"
    r"(?P<scale>[万亿千百萬億])?(?P<suffix>\s*[%‰成])?"
)
_MEASURES = re.compile(r"^(?:个|条|项|次|份|人|名|家|组|类|秒|分钟|分镜|分|小时|时|天|日|周|月|年|"
                       r"度|元|块|万|亿|千|百|米|厘米|毫米|克|千克|公斤|吨|升|毫升|页|章|节|段|批|台|张|"
                       r"倍|个百分点|以上|以下|以内|左右|到|至|和|与|或|及|[KkCcFf%‰])")
_NON_ASSERTION = re.compile(
    r"未(?:采用|使用|写明|列出|提及|出现|包含|确认|确定|明确)|没有|并未|无需|无须|不必|不要求|"
    r"不强制|并非|不是|不采用|可选|示例|举例|例如|比如|省略|仅供参考|待确认|未定|"
    r"(?:旧(?:值|参数|数值)|原(?:值|参数|数值)).{0,48}(?:改为|改成|已改|替换为|调整为)|"
    r"至少|至多|不少于|不低于|不高于|不超过|大于|小于|以上|以下|[<>≤≥]"
)
_UNSUPPORTED = re.compile(
    r"\d\s*(?:[×*/^]|乘以|除以)\s*\d|[±≈~]\s*\d|"
    r"[" + _CN_CHARS + r"](?:来|余|多|几|数)[十百千万亿]|(?:几|数)[十百千万亿]|"
    r"\d\s*[kKmMbB]\s*(?=[条个份件张元人])|(?<![0-9A-Za-z.])\d+(?:\.\d+)?\s*(?:thousand|million|billion)(?![A-Za-z])"
)


def _chinese_integer(text: str) -> Optional[int]:
    if not text:
        return None
    if all(char in _DIGITS for char in text):
        return int("".join(_DIGITS[char] for char in text))
    total = section = number = 0
    last_unit = 0
    digit_pending = False
    zero_after_unit = False
    for char in text:
        if char in _DIGITS:
            digit = int(_DIGITS[char])
            if digit_pending and number and digit:
                return None
            number = digit
            digit_pending = True
            zero_after_unit = zero_after_unit or digit == 0
        elif char in _SMALL_UNITS:
            unit = _SMALL_UNITS[char]
            if last_unit in _SMALL_UNITS.values() and unit >= last_unit:
                return None
            if not digit_pending and not (unit == 10 and section == 0):
                return None
            section += (number if digit_pending else 1) * unit
            number = 0
            digit_pending = zero_after_unit = False
            last_unit = unit
        elif char in _LARGE_UNITS:
            unit = _LARGE_UNITS[char]
            if not section and not number:
                return None
            if unit == 100000000:
                total = (total + section + number) * unit
            else:
                total += (section + number) * unit
            section = number = 0
            digit_pending = zero_after_unit = False
            last_unit = unit
        else:
            return None
    # Colloquial "一百二" / "一万二" has multiple plausible readings.
    if digit_pending and number and last_unit >= 100 and not zero_after_unit:
        return None
    return total + section + number


def _chinese_number(text: str) -> Optional[Decimal]:
    if "点" not in text:
        value = _chinese_integer(text)
        return Decimal(value) if value is not None else None
    if text.count("点") != 1:
        return None
    whole, fraction = text.split("点")
    value = _chinese_integer(whole)
    if value is None or not fraction or any(char not in _DIGITS for char in fraction):
        return None
    return Decimal(str(value) + "." + "".join(_DIGITS[char] for char in fraction))


def _numbers(text: str) -> Tuple[List[Tuple[Decimal, str, int]], bool]:
    """Return known numeric values and whether unsupported notation exists."""
    def fraction_of_ten(match):
        first, second = match.groups()
        first_value = int(_DIGITS.get(first, first))
        second_value = 5 if second == "半" else int(_DIGITS.get(second, second))
        return str(first_value * 10 + second_value) + "%"
    text = re.sub(r"([一二两三四五六七八九1-9])成([一二两三四五六七八九1-9半])", fraction_of_ten, text)
    result = []
    unknown = bool(_UNSUPPORTED.search(text))
    for match in _NUMBER.finditer(text):
        raw = match.group("number")
        prefix, suffix = match.group("prefix"), (match.group("suffix") or "").strip()
        chinese = raw[0] in _CN_CHARS
        if chinese and not prefix and not suffix:
            following = text[match.end():]
            # Do not read lexical "一致" / "一律" / "千万不要" as quantities.
            if following and re.match(r"[\u4e00-\u9fff]", following) and not _MEASURES.match(following):
                continue
            if len(raw) == 1 and not _MEASURES.match(following):
                continue
        if chinese:
            value = _chinese_number(raw)
        else:
            try:
                value = Decimal(raw.replace(",", ""))
            except InvalidOperation:
                value = None
        if value is None:
            unknown = True
            continue
        previous = text[match.start() - 1:match.start()] if match.start() else ""
        if raw.startswith("-") and previous.isdigit():
            value = abs(value)  # 5-8 秒 is a range, not a negative upper bound.
        elif previous == "负":
            value = -value
        scale = match.group("scale")
        if scale:
            value *= Decimal(_LARGE_UNITS.get(scale, _SMALL_UNITS.get(scale, 1)))
        if prefix:
            value /= Decimal({"百分之": 100, "千分之": 1000, "万分之": 10000}[prefix])
        if suffix:
            value /= Decimal({"%": 100, "‰": 1000, "成": 10}[suffix])
        result.append((value, match.group(), match.start()))
    return result, unknown


def validate_coverage_explanation(check: Dict[str, Any], decision: Dict[str, Any],
                                  document: str) -> None:
    """Raise only for a confirmed contradiction in a passing explanation.

    An equal number anywhere in the document is sufficient here: this guard
    does not prove correct scope, units, arithmetic or substantive coverage.
    Unsupported or ambiguous candidate notation remains unknown, never a
    fabricated failure. The ordinary bounded review handles these cases.
    """
    if check.get("passed") is not True or not isinstance(check.get("reason"), str):
        return
    reason = unicodedata.normalize("NFKC", check["reason"])
    source = unicodedata.normalize("NFKC", "\n".join(
        decision[key] for key in ("dimension", "value", "description") if isinstance(decision.get(key), str)
    ))
    candidate = unicodedata.normalize("NFKC", document)
    source_values = {value for value, _, _ in _numbers(source)[0]}
    candidate_numbers, unknown = _numbers(candidate)
    if unknown:
        return
    candidate_values = {value for value, _, _ in candidate_numbers}
    for sentence in re.split(r"[。！？;；\n]", reason):
        if _NON_ASSERTION.search(sentence):
            continue
        for value, raw, _ in _numbers(sentence)[0]:
            if value in source_values and value not in candidate_values:
                raise OperationError(
                    "CREATION_DELIVERY_UNVERIFIED",
                    "通过理由声称正文包含已选参数「{}」，但正文未出现该数值或可确定的等值表达；"
                    "请核对实际正文，不能用输入要求代替产物证据。".format(raw.strip()),
                )
