"""Small, read-only title recall supplement for Brainstorm source discovery."""

import difflib
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List


MAX_SCAN_ROWS = 3000
MAX_TITLE_CANDIDATES = 2
MIN_TITLE_SCORE = 0.52


def _normalized(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9\u3400-\u9fff]", "", str(value or "").lower())
    return re.sub(r"(?:云文档|文档)$", "", normalized)


def _dice(left: Counter, right: Counter) -> float:
    total = sum(left.values()) + sum(right.values())
    return 2.0 * sum((left & right).values()) / total if total else 0.0


def fuzzy_title_score(query: str, title: str) -> float:
    """Combine unordered characters and ordered pairs; tolerate word swaps."""
    left, right = _normalized(query), _normalized(title)
    if len(left) < 8 or len(right) < 4:
        return 0.0
    characters = _dice(Counter(left), Counter(right))
    pairs = _dice(Counter(left[index:index + 2] for index in range(len(left) - 1)),
                  Counter(right[index:index + 2] for index in range(len(right) - 1)))
    sequence = difflib.SequenceMatcher(None, left, right).ratio()
    return 0.40 * characters + 0.45 * pairs + 0.15 * sequence


def _anchors(query: str) -> List[str]:
    normalized = _normalized(query)
    pairs = [normalized[index:index + 2] for index in range(len(normalized) - 1)]
    if not pairs:
        return []
    stride = max(1, len(pairs) // 6)
    return list(dict.fromkeys(pairs[::stride][:6]))


def fuzzy_document_candidates(db_path: str, query: str,
                              limit: int = MAX_TITLE_CANDIDATES) -> List[Dict[str, Any]]:
    """Return at most two source documents, never write or log source text.

    Existing ranked retrieval runs first. This fallback scans only active
    document titles and loads a bounded body excerpt for top-scoring titles.
    It does not infer that a candidate covers the requested period.
    """
    if not isinstance(db_path, str) or not db_path or not isinstance(query, str):
        return []
    if re.match(r"^https?://", query.strip(), re.I):
        return []
    anchors = _anchors(query)
    if len(_normalized(query)) < 8 or not anchors:
        return []
    count = max(0, min(int(limit), MAX_TITLE_CANDIDATES))
    if count == 0:
        return []
    try:
        path = Path(db_path).resolve(strict=True)
        where = " OR ".join("LOWER(title) LIKE ?" for _ in anchors)
        params = ["%" + value + "%" for value in anchors]
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0) as conn:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute(
                "SELECT id,title FROM bake_documents WHERE deleted_at IS NULL "
                "AND (status='enabled' OR status LIKE '%|enabled') "
                "AND LENGTH(title) BETWEEN 4 AND 256 AND (" + where + ") "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                [*params, MAX_SCAN_ROWS],
            ).fetchall()
            ranked = sorted(
                ((fuzzy_title_score(query, title), int(source_id))
                 for source_id, title in rows),
                key=lambda item: (-item[0], item[1]),
            )
            selected = [source_id for score, source_id in ranked
                        if score >= MIN_TITLE_SCORE][:count]
            if not selected:
                return []
            details = conn.execute(
                "SELECT id,title,SUBSTR(full_content,1,12000),SUBSTR(summary,1,1000),"
                "review_status,source_url,updated_at,last_refresh_completeness,"
                "last_refresh_truncated,last_refresh_success_at_ms "
                "FROM bake_documents WHERE id IN (" + ",".join("?" for _ in selected) + ")",
                selected,
            ).fetchall()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return []
    by_id = {int(row[0]): row for row in details}
    candidates = []
    for source_id in selected:
        row = by_id.get(source_id)
        if row is None:
            continue
        candidates.append({
            "id": source_id, "source_id": source_id, "source_type": "document",
            "title": row[1], "full_content": row[2] or row[3] or "",
            "summary": row[3] or "", "review_status": row[4] or "",
            "source_url": row[5], "updated_at": row[6],
            "refresh_completeness": row[7] or "unverified",
            "refresh_truncated": bool(row[8]),
            "observed_at": row[9] or row[6],
        })
    return candidates
