#!/usr/bin/env python3
"""Detach proven cross-window document sources and enqueue them for bake retry.

Dry-run by default. Applying the repair creates a consistent SQLite backup first.
Original captures, timelines, bake runs, and artifact audits remain unchanged.
Python 3.9 compatible.
"""
import argparse
import datetime
import json
import re
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo


def _normalize_title(value):
    title = str(value or "").strip().lower()
    for marker in (" - google chrome", " - microsoft edge", " - brave browser"):
        index = title.find(marker)
        if index >= 0:
            title = title[:index]
    return "".join(char for char in title if char.isalnum())


def _json_ids(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _date_bounds(start_date, end_date, timezone):
    zone = ZoneInfo(timezone)
    start = datetime.datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=zone)
    end = datetime.datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=zone)
    if end <= start:
        raise ValueError("end date must be later than start date")
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _table_rows(conn, table, where, params):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    return [dict(row) for row in conn.execute(
        "SELECT * FROM %s WHERE %s" % (table, where), params
    )]


def _candidate_timeline_ids(conn, document_id, start_ms, end_ms):
    rows = conn.execute(
        """
        SELECT DISTINCT a.timeline_id
        FROM bake_artifact_audits a
        JOIN timelines t ON t.id = a.timeline_id
        WHERE a.artifact_kind = 'document'
          AND a.persist_status = 'created'
          AND a.artifact_id = ?
          AND COALESCE(t.start_time, t.observed_at, t.created_at_ms) >= ?
          AND COALESCE(t.start_time, t.observed_at, t.created_at_ms) < ?
        ORDER BY a.timeline_id
        """,
        (document_id, start_ms, end_ms),
    )
    return [int(row[0]) for row in rows]


def _has_proven_cross_window_capture(conn, document, timeline_id):
    document_title = _normalize_title(document["title"])
    source_url = str(document["source_url"] or "").strip()
    if not document_title or not source_url:
        return False
    for row in conn.execute(
        """
        SELECT url, webpage_title, win_title, ax_text, ocr_text
        FROM captures
        WHERE timeline_id = ? OR id = (
            SELECT capture_id FROM timelines WHERE id = ?
        )
        """,
        (timeline_id, timeline_id),
    ):
        page_title = _normalize_title(row["webpage_title"])
        window_title = _normalize_title(row["win_title"])
        visible_chars = len(re.sub(r"\s+", "", str(row["ax_text"] or row["ocr_text"] or "")))
        if (
            str(row["url"] or "").strip() == source_url
            and page_title == document_title
            and window_title
            and window_title != document_title
            and visible_chars >= 200
        ):
            return True
    return False


def _already_recovered_elsewhere(conn, document_id, timeline_id):
    """Return whether the source is already bound to another active document."""
    return conn.execute(
        """
        SELECT 1
        FROM bake_documents d
        WHERE d.id <> ?
          AND d.deleted_at IS NULL
          AND (
              EXISTS (
                  SELECT 1
                  FROM bake_document_source_fingerprints f
                  WHERE f.document_id = d.id
                    AND f.source_timeline_id = ?
              )
              OR EXISTS (
                  SELECT 1
                  FROM json_each(
                      CASE WHEN json_valid(d.source_memory_ids)
                           THEN d.source_memory_ids ELSE '[]' END
                  ) source
                  WHERE CAST(source.value AS TEXT) = CAST(? AS TEXT)
              )
              OR EXISTS (
                  SELECT 1
                  FROM json_each(
                      CASE WHEN json_valid(d.source_episode_ids)
                           THEN d.source_episode_ids ELSE '[]' END
                  ) source
                  WHERE CAST(source.value AS TEXT) = CAST(? AS TEXT)
              )
          )
        LIMIT 1
        """,
        (document_id, timeline_id, timeline_id, timeline_id),
    ).fetchone() is not None


def _quality_rejected_after_polluted_link(conn, document_id, timeline_id):
    """Return whether a later repaired pass conclusively rejected the source."""
    polluted = conn.execute(
        """
        SELECT MAX(id)
        FROM bake_artifact_audits
        WHERE timeline_id=? AND artifact_kind='document'
          AND persist_status='created' AND artifact_id=?
        """,
        (timeline_id, document_id),
    ).fetchone()[0]
    if polluted is None:
        return False
    latest = conn.execute(
        """
        SELECT persist_status
        FROM bake_artifact_audits
        WHERE timeline_id=? AND artifact_kind='document' AND id>?
        ORDER BY id DESC LIMIT 1
        """,
        (timeline_id, polluted),
    ).fetchone()
    return latest is not None and latest[0] == "rejected"


def inspect(conn, document_id, start_ms, end_ms):
    document = conn.execute(
        "SELECT * FROM bake_documents WHERE id=? AND deleted_at IS NULL", (document_id,)
    ).fetchone()
    if document is None:
        raise ValueError("active document not found")
    if document["creation_mode"] != "llm_bake":
        raise ValueError("target must be an auto-created bake document")
    candidates = _candidate_timeline_ids(conn, document_id, start_ms, end_ms)
    proven = [
        timeline_id
        for timeline_id in candidates
        if _has_proven_cross_window_capture(conn, document, timeline_id)
    ]
    recovered = [
        timeline_id
        for timeline_id in proven
        if _already_recovered_elsewhere(conn, document_id, timeline_id)
    ]
    recovered_set = set(recovered)
    quality_rejected = [
        timeline_id
        for timeline_id in proven
        if timeline_id not in recovered_set
        and _quality_rejected_after_polluted_link(conn, document_id, timeline_id)
    ]
    resolved_set = recovered_set.union(quality_rejected)
    selected = [item for item in proven if item not in resolved_set]
    return dict(document), candidates, proven, recovered, quality_rejected, selected


def recover(db_path, document_id, start_ms, end_ms, backup_root, apply=False):
    db_path = Path(db_path).resolve()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    document, candidates, proven, recovered, quality_rejected, selected = inspect(
        conn, document_id, start_ms, end_ms
    )
    report = {
        "document_id": document_id,
        "audited_timeline_count": len(candidates),
        "proven_cross_window_count": len(proven),
        "already_recovered_count": len(recovered),
        "already_recovered_timeline_ids": recovered,
        "quality_rejected_count": len(quality_rejected),
        "quality_rejected_timeline_ids": quality_rejected,
        "pending_recovery_count": len(selected),
        "timeline_ids": selected,
        "applied": False,
    }
    if not apply or (not selected and not recovered and not quality_rejected):
        conn.close()
        return report

    backup_root = Path(backup_root).resolve()
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = time.time_ns()
    backup_db = backup_root / (
        "cross-window-document-%s-%s.db" % (document_id, stamp)
    )
    with sqlite3.connect(str(backup_db)) as backup_conn:
        conn.backup(backup_conn)

    placeholders = ",".join("?" for _ in selected)
    resolved = recovered + quality_rejected
    retry_scope = selected + resolved
    retry_placeholders = ",".join("?" for _ in retry_scope)
    selected_strings = set(str(item) for item in selected)
    capture_ids = []
    if selected:
        capture_ids = [
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM captures WHERE timeline_id IN (%s)" % placeholders,
                selected,
            )
        ]
    capture_id_set = set(capture_ids)
    audit_path = backup_root / (
        "cross-window-document-%s-%s.json" % (document_id, stamp)
    )
    audit_payload = {
        "report": report,
        "document_before": document,
        "fingerprints_before": _table_rows(
            conn,
            "bake_document_source_fingerprints",
            "document_id=? AND source_timeline_id IN (%s)" % placeholders,
            [document_id] + selected,
        ),
        "retry_rows_before": _table_rows(
            conn,
            "bake_retry_state",
            "timeline_id IN (%s)" % retry_placeholders,
            retry_scope,
        ),
    }
    audit_path.write_text(
        json.dumps(audit_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    now_ms = int(time.time() * 1000)
    source_memory_ids = [
        item for item in _json_ids(document["source_memory_ids"])
        if item not in selected_strings
    ]
    source_episode_ids = [
        item for item in _json_ids(document["source_episode_ids"])
        if item not in selected_strings
    ]
    source_capture_ids = [
        item for item in _json_ids(document["source_capture_ids"])
        if item not in capture_id_set
    ]

    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        UPDATE bake_documents
        SET source_memory_ids=?, source_episode_ids=?, source_capture_ids=?, updated_at=?
        WHERE id=? AND deleted_at IS NULL
        """,
        (
            json.dumps(source_memory_ids, ensure_ascii=False),
            json.dumps(source_episode_ids, ensure_ascii=False),
            json.dumps(source_capture_ids, ensure_ascii=False),
            now_ms,
            document_id,
        ),
    )
    if selected:
        conn.execute(
            "DELETE FROM bake_document_source_fingerprints "
            "WHERE document_id=? AND source_timeline_id IN (%s)" % placeholders,
            [document_id] + selected,
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='bake_document_refresh_observations'"
        ).fetchone():
            conn.execute(
                """
                UPDATE bake_document_refresh_observations
                SET state='blocked', lease_id=NULL, lease_until=0,
                    last_error='SOURCE_IDENTITY_MISMATCH', updated_at=?
                WHERE document_id=? AND source_timeline_id IN (%s)
                """ % placeholders,
                [now_ms, document_id] + selected,
            )
    for timeline_id in selected:
        conn.execute(
            """
            INSERT INTO bake_retry_state
                (timeline_id, failure_count, last_error, last_failed_at_ms,
                 last_error_code, next_retry_at_ms)
            VALUES (?, 1, 'cross-window source identity detached', ?,
                    'DOCUMENT_SOURCE_IDENTITY_MISMATCH', 0)
            ON CONFLICT(timeline_id) DO UPDATE SET
                failure_count=1,
                last_error=excluded.last_error,
                last_failed_at_ms=excluded.last_failed_at_ms,
                last_error_code=excluded.last_error_code,
                next_retry_at_ms=0
            WHERE COALESCE(bake_retry_state.last_error_code, '') <>
                  'DOCUMENT_SOURCE_IDENTITY_MISMATCH'
            """,
            (timeline_id, now_ms),
        )
    if resolved:
        resolved_placeholders = ",".join("?" for _ in resolved)
        conn.execute(
            "DELETE FROM bake_retry_state "
            "WHERE last_error_code='DOCUMENT_SOURCE_IDENTITY_MISMATCH' "
            "AND timeline_id IN (%s)" % resolved_placeholders,
            resolved,
        )
    conn.commit()
    conn.close()
    report.update({
        "applied": True,
        "backup_db": str(backup_db),
        "audit_file": str(audit_path),
        "detached_capture_count": len(capture_id_set),
        "cleared_resolved_retry_count": len(resolved),
    })
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--document-id", type=int, required=True)
    parser.add_argument("--from-date", required=True)
    parser.add_argument("--to-date", required=True, help="exclusive local date")
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    start_ms, end_ms = _date_bounds(args.from_date, args.to_date, args.timezone)
    print(json.dumps(recover(
        args.db,
        args.document_id,
        start_ms,
        end_ms,
        args.backup_root,
        args.apply,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
