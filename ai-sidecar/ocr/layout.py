"""Keep spatially aligned OCR cells together for interactive image questions."""

import math
from statistics import median
from typing import List, Optional, Tuple

from .backends.base import OcrBox


def _rect(box: OcrBox) -> Optional[Tuple[float, float, float, float]]:
    try:
        points = [(float(p[0]), float(p[1])) for p in box.bbox]
        if len(points) < 4 or not all(math.isfinite(v) for p in points for v in p):
            return None
        xs, ys = zip(*points)
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom
    except (ValueError, TypeError, IndexError):
        return None


def reading_order_text(boxes: List[OcrBox]) -> str:
    """Restore rows without guessing cell values or a table's column meanings.

    Vision may return all left-hand cells before the numeric columns. Group by
    vertical alignment, then order within each row by x. The geometry is scale
    independent, supporting both normalized Vision and pixel Paddle boxes.
    Missing geometry falls back to the backend order, preserving all text.
    """
    populated = [box for box in boxes if box.text.strip()]
    cells = [(box, _rect(box)) for box in populated]
    if not cells or any(rect is None for _, rect in cells):
        return "\n".join(box.text for box in populated)

    rows = []
    for box, rect in sorted(cells, key=lambda cell: ((cell[1][1] + cell[1][3]) / 2, cell[1][0])):
        center = (rect[1] + rect[3]) / 2
        height = rect[3] - rect[1]
        row = rows[-1] if rows else None
        if row is not None:
            row_center = median((item[1][1] + item[1][3]) / 2 for item in row)
            row_height = median(item[1][3] - item[1][1] for item in row)
            same_line = abs(center - row_center) <= min(height, row_height) * 0.55
        else:
            same_line = False
        if same_line:
            row.append((box, rect))
        else:
            rows.append([(box, rect)])

    lines = []
    for row in rows:
        ordered = sorted(row, key=lambda cell: cell[1][0])
        # Preserve detection-box boundaries explicitly. Do not compare x gaps
        # with y heights: normalized coordinates have different aspect ratios.
        lines.append(" | ".join(item[0].text.strip() for item in ordered))
    return "\n".join(lines)
