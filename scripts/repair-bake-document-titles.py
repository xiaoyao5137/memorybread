#!/usr/bin/env python3
"""从已关联的原始采集记录修复烘焙文档占位标题。

默认只审计；传入 ``--apply`` 才会写库，写入前使用 SQLite backup API 创建一致性备份。
标题候选仅来自 bake_documents.source_capture_ids，避免用无来源的模型猜测覆盖用户数据。
"""

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote


DEFAULT_DB_PATH = Path.home() / ".memory-bread" / "memory-bread.db"
GENERIC_IDENTITIES = {
    "docs",
    "document",
    "documents",
    "文档",
    "云文档",
    "在线文档",
    "untitled",
    "untitleddocument",
    "无标题",
    "无标题文档",
    "未命名",
    "未命名文档",
    "知识库",
    "knowledgebase",
    "googlechrome",
    "microsoftedge",
    "safari",
    "firefox",
    "microsoftword",
    "word",
    "pages",
    "kim",
    "chatgpt",
    "snip",
}
RUNTIME_SUFFIXES = (
    " - google chrome",
    " - microsoft edge",
    " - safari",
    " - firefox",
    " - arc",
    " - microsoft word",
    " - word",
    " - visual studio code",
    " - cursor",
    " - pages",
)
CLOUD_SUFFIXES = ("-云文档", "（云文档）", "(云文档)")
MEMORY_SUFFIX_RE = re.compile(
    r"\s+-\s+内存用量高\s+-\s+[\d,.]+\s*(?:mb|gb)\s*$", re.IGNORECASE
)


def clean_runtime_title(value: Any) -> str:
    title = " ".join(str(value or "").split()).strip()
    while title:
        lowered = title.lower()
        suffix = next(
            (candidate for candidate in RUNTIME_SUFFIXES if lowered.endswith(candidate)),
            None,
        )
        if suffix is None:
            break
        title = title[: -len(suffix)].strip()
    title = MEMORY_SUFFIX_RE.sub("", title).strip()
    return title


def title_identity(value: Any) -> str:
    normalized = clean_runtime_title(value).lower()
    normalized = "".join(normalized.split()).replace("–", "-").replace("—", "-").replace("－", "-")
    while normalized:
        suffix = next(
            (candidate for candidate in CLOUD_SUFFIXES if normalized.endswith(candidate)),
            None,
        )
        if suffix is None:
            break
        normalized = normalized[: -len(suffix)].rstrip("-")
    return normalized


def is_generic_title(value: Any) -> bool:
    identity = title_identity(value)
    return len(identity) < 3 or identity in GENERIC_IDENTITIES


def source_title(value: Any, app_name: Any) -> Optional[str]:
    title = clean_runtime_title(value)
    if not title or is_generic_title(title):
        return None
    if app_name and title.lower() == str(app_name).strip().lower():
        return None
    return title


def parse_capture_ids(raw: Any) -> List[int]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    result: List[int] = []
    for item in value:
        try:
            capture_id = int(item)
        except (TypeError, ValueError):
            continue
        if capture_id not in result:
            result.append(capture_id)
    return result


def chunks(values: Sequence[int], size: int = 800) -> Iterable[Sequence[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def load_captures(conn: sqlite3.Connection, capture_ids: Sequence[int]) -> List[sqlite3.Row]:
    rows: List[sqlite3.Row] = []
    for part in chunks(capture_ids):
        placeholders = ",".join("?" for _ in part)
        rows.extend(
            conn.execute(
                "SELECT id, ts, app_name, webpage_title, win_title "
                "FROM captures WHERE id IN ({}) ORDER BY ts ASC, id ASC".format(placeholders),
                tuple(part),
            ).fetchall()
        )
    return rows


def preferred_title(captures: Sequence[sqlite3.Row]) -> Optional[str]:
    candidates: Dict[str, Dict[str, Any]] = {}
    for capture in captures:
        identities_in_capture = set()
        for column, is_webpage in (("webpage_title", True), ("win_title", False)):
            display = source_title(capture[column], capture["app_name"])
            if display is None:
                continue
            identity = title_identity(display)
            if not identity or identity in identities_in_capture:
                continue
            identities_in_capture.add(identity)
            candidate = candidates.setdefault(
                identity,
                {
                    "display": display,
                    "capture_count": 0,
                    "latest_ts": int(capture["ts"] or 0),
                    "has_webpage_title": is_webpage,
                },
            )
            candidate["capture_count"] += 1
            current_rank = (
                int(candidate["latest_ts"]),
                bool(candidate["has_webpage_title"]),
            )
            next_rank = (int(capture["ts"] or 0), is_webpage)
            if next_rank > current_rank:
                candidate["display"] = display
                candidate["latest_ts"] = int(capture["ts"] or 0)
                candidate["has_webpage_title"] = is_webpage

    if not candidates:
        return None
    selected = max(
        candidates.values(),
        key=lambda item: (
            int(item["capture_count"]),
            int(item["latest_ts"]),
            bool(item["has_webpage_title"]),
            len(str(item["display"])),
        ),
    )
    return str(selected["display"])


def audit(conn: sqlite3.Connection, document_ids: Sequence[int]) -> List[Dict[str, Any]]:
    sql = (
        "SELECT id, title, source_win_title, source_capture_ids "
        "FROM bake_documents WHERE deleted_at IS NULL"
    )
    params: Tuple[Any, ...] = ()
    if document_ids:
        sql += " AND id IN ({})".format(",".join("?" for _ in document_ids))
        params = tuple(document_ids)
    sql += " ORDER BY id"

    repairs: List[Dict[str, Any]] = []
    for document in conn.execute(sql, params).fetchall():
        if not is_generic_title(document["title"]):
            continue
        capture_ids = parse_capture_ids(document["source_capture_ids"])
        replacement = preferred_title(load_captures(conn, capture_ids))
        if replacement is None or is_generic_title(replacement):
            continue
        repairs.append(
            {
                "id": int(document["id"]),
                "old_title": str(document["title"]),
                "new_title": replacement,
                "old_source_win_title": document["source_win_title"],
                "capture_count": len(capture_ids),
            }
        )
    return repairs


def backup_database(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / "memory-bread-before-document-title-repair-{}.db".format(
        time.strftime("%Y%m%d-%H%M%S")
    )
    source = sqlite3.connect(str(db_path), timeout=30)
    target = sqlite3.connect(str(backup_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def apply_repairs(conn: sqlite3.Connection, repairs: Sequence[Dict[str, Any]]) -> int:
    updated = 0
    now_ms = int(time.time() * 1000)
    with conn:
        for repair in repairs:
            source_win_title = repair["old_source_win_title"]
            if is_generic_title(source_win_title):
                source_win_title = repair["new_title"]
            cursor = conn.execute(
                "UPDATE bake_documents "
                "SET title = ?1, source_win_title = ?2, updated_at = ?3 "
                "WHERE id = ?4 AND title = ?5 AND deleted_at IS NULL",
                (
                    repair["new_title"],
                    source_win_title,
                    now_ms,
                    repair["id"],
                    repair["old_title"],
                ),
            )
            updated += cursor.rowcount
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--ids", type=int, nargs="*", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = args.db.expanduser().resolve()
    if not db_path.is_file():
        parser.error("数据库不存在: {}".format(db_path))

    if args.apply:
        conn = sqlite3.connect(str(db_path), timeout=30)
    else:
        uri = "file:{}?mode=ro".format(quote(str(db_path)))
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        repairs = audit(conn, args.ids)
        for repair in repairs:
            print(
                "document {id}: {old_title!r} -> {new_title!r} "
                "(source captures: {capture_count})".format(**repair)
            )
        if not args.apply:
            print("dry-run: {} document(s) can be repaired".format(len(repairs)))
            return 0
        if not repairs:
            print("apply: no repair needed")
            return 0

        backup_path = backup_database(db_path)
        updated = apply_repairs(conn, repairs)
        print("backup: {}".format(backup_path))
        print("apply: repaired {} document(s)".format(updated))
        return 0 if updated == len(repairs) else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
