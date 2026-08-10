#!/usr/bin/env python3
"""用已落库操作回放多帧守卫；全程只读且不调用大模型。"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote


REPO_ROOT = Path(__file__).resolve().parents[1]
SIDECAR_ROOT = REPO_ROOT / "ai-sidecar"
if str(SIDECAR_ROOT) not in sys.path:
    sys.path.insert(0, str(SIDECAR_ROOT))

from knowledge.extractor_v2 import KnowledgeExtractorV2  # noqa: E402


DEFAULT_DB = (
    Path.home()
    / "Library"
    / "Application Support"
    / "com.memory-bread.app"
    / "runtime"
    / ".memory-bread"
    / "memory-bread.db"
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


def _capture_context(conn: sqlite3.Connection, capture_ids: List[int]) -> str:
    if not capture_ids:
        return ""
    placeholders = ",".join("?" for _ in capture_ids)
    rows = conn.execute(
        "SELECT id, ts, ax_text, ocr_text, input_text, audio_text "
        "FROM captures WHERE id IN ({}) ORDER BY ts, id".format(placeholders),
        capture_ids,
    ).fetchall()
    blocks = []
    for row in rows:
        text = "\n".join(
            str(row[key] or "").strip()
            for key in ("ax_text", "ocr_text", "input_text", "audio_text")
            if str(row[key] or "").strip()
        )
        if text:
            blocks.append("--- capture#{} ts={} ---\n{}".format(row["id"], row["ts"], text))
    return "\n\n".join(blocks)


def _candidate_from_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    capture_ids: List[int],
) -> Dict[str, Any]:
    primary = conn.execute(
        "SELECT ts, app_name, win_title, ax_text, ocr_text, input_text, audio_text, "
        "url, webpage_title FROM captures WHERE id = ?",
        (row["capture_id"],),
    ).fetchone()
    context = _capture_context(conn, capture_ids)
    return {
        "source_timeline_id": row["timeline_id"],
        "source_capture_id": row["capture_id"],
        "source_capture_count": len(capture_ids),
        "summary": row["timeline_summary"],
        "overview": row["timeline_overview"],
        "details": row["timeline_details"],
        "entities": [],
        "importance": row["importance"],
        "capture_ts": primary["ts"] if primary else 0,
        "capture_app_name": primary["app_name"] if primary else None,
        "capture_win_title": primary["win_title"] if primary else None,
        "capture_ax_text": primary["ax_text"] if primary else None,
        "capture_ocr_text": primary["ocr_text"] if primary else None,
        "capture_input_text": primary["input_text"] if primary else None,
        "capture_audio_text": primary["audio_text"] if primary else None,
        "capture_url": primary["url"] if primary else None,
        "capture_webpage_title": primary["webpage_title"] if primary else None,
        "url_aggregated_text": context,
        "url_aggregated_capture_count": len(capture_ids),
    }


def replay(db_path: Path, limit: int) -> Dict[str, Any]:
    uri = "file:{}?mode=ro".format(quote(str(db_path.resolve())))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT s.id, s.timeline_id, s.title, s.content, s.source_capture_ids, "
        "t.capture_id, t.summary AS timeline_summary, t.overview AS timeline_overview, "
        "t.details AS timeline_details, t.importance "
        "FROM bake_sops s JOIN timelines t ON t.id = s.timeline_id "
        "ORDER BY s.created_at_ms DESC, s.id DESC LIMIT ?",
        (limit,),
    ).fetchall()

    extractor = KnowledgeExtractorV2.__new__(KnowledgeExtractorV2)
    extractor.model = "recorded-replay"
    results = []
    for row in rows:
        capture_ids = _parse_ids(row["source_capture_ids"])
        candidate = _candidate_from_row(conn, row, capture_ids)
        try:
            payload = json.loads(row["content"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        parsed = {"accepted": True, "reason": "recorded", "payload": payload}
        meta = {"elapsed_ms": 0, "usage": None, "model": "recorded-replay"}

        historical, _ = extractor._normalize_bake_artifact_result(
            candidate,
            "sop",
            parsed,
            meta,
            caller_id="replay:{}".format(row["id"]),
        )
        single_frame, _ = extractor._normalize_bake_artifact_result(
            dict(candidate, source_capture_count=1),
            "sop",
            parsed,
            meta,
            caller_id="replay-single:{}".format(row["id"]),
        )
        results.append(
            {
                "sop_id": row["id"],
                "title": row["title"],
                "source_capture_count": len(capture_ids),
                "historical_multi_frame_accepted": historical["accepted"],
                "historical_reason": historical.get("reason"),
                "single_frame_replay_rejected": not single_frame["accepted"],
                "single_frame_reason": single_frame.get("reason"),
            }
        )

    conn.close()
    passed = all(
        item["source_capture_count"] >= 2
        and item["historical_multi_frame_accepted"]
        and item["single_frame_replay_rejected"]
        and item["single_frame_reason"] == "insufficient_multi_capture_evidence"
        for item in results
    )
    return {
        "database": str(db_path),
        "model_inference_calls": 0,
        "replayed_operations": len(results),
        "passed": passed and bool(results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("数据库不存在: {}".format(args.db))
    report = replay(args.db, max(1, args.limit))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
