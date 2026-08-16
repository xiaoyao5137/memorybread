#!/usr/bin/env python3
"""审计历史 SOP 的真实动作证据；默认只读，显式 --apply 才治理高置信误产物。"""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote


LEGACY_DB = Path.home() / ".memory-bread" / "memory-bread.db"
APP_DB = (
    Path.home()
    / "Library"
    / "Application Support"
    / "com.memory-bread.app"
    / "runtime"
    / ".memory-bread"
    / "memory-bread.db"
)
DEFAULT_DB = LEGACY_DB if LEGACY_DB.is_file() else APP_DB
PASSIVE_EVENTS = {"app_switch", "browser_navigation", "auto", "key_pause", "scroll"}
AGENT_SURFACE_MARKERS = (
    "myflicker",
    "codeflicker",
    "workbuddy",
    "chatgpt",
    "claude",
    "copilot",
)


def _parse_ids(value: Any) -> List[int]:
    try:
        items = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(result))


def _parse_content(value: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _capture_ids(row: sqlite3.Row, content: Dict[str, Any]) -> List[int]:
    return _parse_ids(content.get("source_capture_ids")) or _parse_ids(
        row["source_capture_ids"]
    )


def _load_captures(
    conn: sqlite3.Connection, capture_ids: Sequence[int]
) -> List[sqlite3.Row]:
    if not capture_ids:
        return []
    placeholders = ",".join("?" for _ in capture_ids)
    return conn.execute(
        "SELECT id, ts, event_type, app_name, win_title, input_text, "
        "ax_focused_role, ax_focused_id "
        "FROM captures WHERE id IN ({}) ORDER BY ts, id".format(placeholders),
        tuple(capture_ids),
    ).fetchall()


def _is_agent_surface(capture: sqlite3.Row) -> bool:
    surface = "{} {}".format(
        capture["app_name"] or "", capture["win_title"] or ""
    ).lower()
    return any(marker in surface for marker in AGENT_SURFACE_MARKERS)


def _has_input(capture: sqlite3.Row) -> bool:
    return bool(str(capture["input_text"] or "").strip())


def _has_favorite(conn: sqlite3.Connection, sop_id: int) -> bool:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_favorites'"
    ).fetchone()
    if not table_exists:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM memory_favorites "
            "WHERE resource_kind = 'operation' AND resource_id = ?",
            (sop_id,),
        ).fetchone()
        is not None
    )


def _audit_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    content = _parse_content(row["content"])
    capture_ids = _capture_ids(row, content)
    captures = _load_captures(conn, capture_ids)
    event_types = [str(capture["event_type"] or "auto") for capture in captures]
    input_capture_ids = [capture["id"] for capture in captures if _has_input(capture)]
    agent_capture_ids = [capture["id"] for capture in captures if _is_agent_surface(capture)]
    passive_only = bool(captures) and all(
        event_type in PASSIVE_EVENTS for event_type in event_types
    )
    agent_surface = bool(agent_capture_ids)
    no_real_input = not input_capture_ids
    user_edited = bool(row["user_edited"])
    favorite = _has_favorite(conn, int(row["id"]))
    creation_mode = str(content.get("creation_mode") or "unknown")
    generated = creation_mode == "llm_bake"
    high_confidence = (
        generated
        and not user_edited
        and not favorite
        and passive_only
        and agent_surface
        and no_real_input
    )
    if high_confidence:
        reason = "passive_agent_report_only"
    elif user_edited:
        reason = "protected_user_edited"
    elif favorite:
        reason = "protected_favorite"
    elif input_capture_ids:
        reason = "has_real_input"
    elif not agent_surface:
        reason = "not_agent_surface"
    elif not passive_only:
        reason = "has_non_passive_event"
    else:
        reason = "insufficient_confidence"
    return {
        "sop_id": row["id"],
        "timeline_id": row["timeline_id"],
        "title": row["title"],
        "source_capture_ids": capture_ids,
        "event_types": event_types,
        "input_capture_ids": input_capture_ids,
        "agent_capture_ids": agent_capture_ids,
        "user_edited": user_edited,
        "favorite": favorite,
        "creation_mode": creation_mode,
        "high_confidence_invalid": high_confidence,
        "reason": reason,
    }


def audit(db_path: Path, limit: int, sop_ids: Sequence[int]) -> Dict[str, Any]:
    uri = "file:{}?mode=ro".format(quote(str(db_path.resolve())))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    where = ""
    params: List[Any] = []
    if sop_ids:
        placeholders = ",".join("?" for _ in sop_ids)
        where = "WHERE s.id IN ({})".format(placeholders)
        params.extend(sop_ids)
    params.append(limit)
    rows = conn.execute(
        "SELECT s.id, s.timeline_id, s.title, s.content, s.source_capture_ids, "
        "s.user_edited FROM bake_sops s {} "
        "ORDER BY s.created_at_ms DESC, s.id DESC LIMIT ?".format(where),
        tuple(params),
    ).fetchall()
    results = [_audit_row(conn, row) for row in rows]
    conn.close()
    return {
        "database": str(db_path),
        "mode": "dry-run",
        "audited_operations": len(results),
        "high_confidence_invalid_count": sum(
            1 for item in results if item["high_confidence_invalid"]
        ),
        "results": results,
    }


def _backup_database(db_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = db_path.with_name("{}.sop-guard-{}.bak".format(db_path.name, timestamp))
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(backup_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def apply_governance(
    db_path: Path, report: Dict[str, Any], requested_ids: Sequence[int]
) -> Dict[str, Any]:
    eligible = {
        int(item["sop_id"])
        for item in report["results"]
        if item["high_confidence_invalid"]
    }
    requested = set(int(item) for item in requested_ids)
    blocked = sorted(requested - eligible)
    if blocked:
        raise ValueError(
            "以下 SOP 未通过高置信安全门禁，拒绝删除: {}".format(blocked)
        )
    backup_path = _backup_database(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        for sop_id in sorted(requested):
            conn.execute(
                "DELETE FROM bake_artifact_source_fingerprints "
                "WHERE artifact_kind = 'sop' AND artifact_id = ?",
                (sop_id,),
            )
            conn.execute(
                "DELETE FROM bake_artifact_source_links "
                "WHERE artifact_kind = 'sop' AND artifact_id = ?",
                (sop_id,),
            )
            if conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'memory_favorites'"
            ).fetchone():
                conn.execute(
                    "DELETE FROM memory_favorites "
                    "WHERE resource_kind = 'operation' AND resource_id = ?",
                    (sop_id,),
                )
            conn.execute("DELETE FROM bake_sops WHERE id = ?", (sop_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        **report,
        "mode": "apply",
        "backup": str(backup_path),
        "deleted_sop_ids": sorted(requested),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--sop-id", action="append", type=int, default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("数据库不存在: {}".format(args.db))
    if args.apply and not args.sop_id:
        parser.error("--apply 必须同时显式提供至少一个 --sop-id")
    report = audit(args.db, max(1, args.limit), args.sop_id)
    if args.apply:
        try:
            report = apply_governance(args.db, report, args.sop_id)
        except ValueError as error:
            parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
