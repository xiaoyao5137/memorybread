"""Conservative source-span resolution for model-quoted evidence (Python 3.9)."""

import re
import unicodedata
from typing import List, Optional


def typography_key(text: str) -> tuple[str, List[int]]:
    """Map only horizontal Han/ASCII spacing back to original character offsets.

    Word/number separators, line breaks, punctuation and case remain significant.
    Only use this key to locate quoted text; never rewrite instructions,
    document edits or factual values with it.
    """
    chars: List[str] = []
    offsets: List[int] = []
    cursor = 0

    def horizontal(char: str) -> bool:
        return char == "\t" or unicodedata.category(char) == "Zs"

    def han(char: str) -> bool:
        return unicodedata.name(char, "").startswith(
            ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")
        )

    def latin_number(char: str) -> bool:
        return char.isascii() and char.isalnum()

    while cursor < len(text):
        end = cursor
        if horizontal(text[cursor]):
            while end < len(text) and horizontal(text[end]):
                end += 1
            if cursor > 0 and end < len(text):
                left, right = text[cursor - 1], text[end]
                if (han(left) and latin_number(right)) or (latin_number(left) and han(right)):
                    cursor = end
                    continue
            chars.extend(text[cursor:end])
            offsets.extend(range(cursor, end))
            cursor = end
            continue
        chars.append(text[cursor])
        offsets.append(cursor)
        cursor += 1
    return "".join(chars), offsets


def _emphasis_typography_key(text: str) -> tuple[str, List[int]]:
    """Ignore paired inline bold delimiters only, keeping original offsets."""
    ignored = set()
    if "`" not in text:
        for match in re.finditer(r"(?<![A-Za-z0-9\\*])\*\*(\S(?:[^\n\r*]*?\S)?)\*\*(?![A-Za-z0-9*])", text):
            ignored.update((match.start(), match.start() + 1, match.end() - 2, match.end() - 1))
    offsets = [index for index in range(len(text)) if index not in ignored]
    key, key_offsets = typography_key("".join(text[index] for index in offsets))
    return key, [offsets[index] for index in key_offsets]


def resolve_source_span(instruction: str, quote: str, require_unique: bool = False,
                        allow_emphasis: bool = False) -> Optional[tuple[int, int]]:
    """Prefer verbatim evidence; typography recovery must identify one source span."""
    start = instruction.find(quote)
    if start >= 0:
        if require_unique and instruction.find(quote, start + 1) >= 0:
            return None
        return start, start + len(quote)
    key_fn = _emphasis_typography_key if allow_emphasis else typography_key
    source_key, offsets = key_fn(instruction)
    quote_key, _ = key_fn(quote)
    start = source_key.find(quote_key) if quote_key else -1
    if start < 0 or source_key.find(quote_key, start + 1) >= 0:
        return None
    return offsets[start], offsets[start + len(quote_key) - 1] + 1

