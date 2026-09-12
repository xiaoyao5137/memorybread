"""Count rejected source versions after the caller's SQLite transaction ends."""
from collections import Counter
from contextvars import ContextVar
from functools import wraps
import logging
import sqlite3
import time

logger = logging.getLogger(__name__)
_events = ContextVar("document_source_mismatches", default=None)
_components = {"rag", "vector_write", "creation", "vector_schedule"}
_reasons = {"head_invalid", "snapshot_mismatch", "index_version_mismatch", "body_mismatch", "document_missing", "summary_version_mismatch"}


def record_unverified_heads(conn, rows):
    """Audit only evaluated rows whose current-source projection was rejected."""
    record_unverified_summaries(conn, rows)
    ids = sorted({row['id'] for row in rows if type(row.get('id')) is int
                  and row.get('source_snapshot_id') is None})
    if not ids or not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bake_document_source_heads'").fetchone():
        return
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        for row in conn.execute("SELECT document_id,snapshot_id FROM bake_document_source_heads WHERE document_id IN ("
                                + ','.join('?' for _ in batch) + ')', batch):
            record_source_mismatch(row[0], 'head_invalid', expected=row[1])


def record_unverified_summaries(conn, rows):
    from embedding.document_source import source_summary_select
    ids = sorted({row['id'] for row in rows if type(row.get('id')) is int})
    if not ids or not conn.execute("SELECT 1 FROM sqlite_master WHERE name='bake_document_source_heads'").fetchone():
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(bake_documents)")}
    if "summary" not in columns:
        return
    observed = "d.summary_source_snapshot_id" if "summary_source_snapshot_id" in columns else "NULL"
    summary = source_summary_select(conn)
    for offset in range(0, len(ids), 500):
        batch = ids[offset:offset + 500]
        for row in conn.execute("SELECT d.id,h.snapshot_id," + observed
                                + " FROM bake_documents d LEFT JOIN bake_document_source_heads h ON h.document_id=d.id"
                                + " WHERE d.id IN (" + ','.join('?' for _ in batch) + ")"
                                + " AND COALESCE(d.summary,'')<>'' AND (" + summary + ") IS NULL", batch):
            record_source_mismatch(row[0], 'summary_version_mismatch', expected=row[1], observed=row[2])


def record_source_mismatch(document_id, reason, expected=None, observed=None):
    scope = _events.get()
    if scope is None or type(document_id) is not int or document_id <= 0 or reason not in _reasons:
        return
    component, events = scope
    # Invalid types never become telemetry strings or source identities.
    expected = expected if type(expected) is int and expected > 0 else None
    observed = observed if type(observed) is int and observed > 0 else None
    events[(document_id, component, reason, expected, observed)] += 1


def audit_source_mismatches(component):
    if component not in _components:
        raise ValueError("Unsupported source audit component")

    def decorate(function):
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            events = Counter()
            token = _events.set((component, events))
            try:
                return function(self, *args, **kwargs)
            finally:
                _events.reset(token)
                if events:
                    _persist(getattr(self, "db_path", None), events)
        return wrapped
    return decorate


def _persist(db_path, events):
    # Called after return unwinds all read/write context managers. A separate
    # short transaction avoids upgrading the caller's read snapshot to a writer.
    if not db_path:
        return
    try:
        with sqlite3.connect(db_path, timeout=0.1) as conn:
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='document_source_mismatch_events'").fetchone()
            if not schema:
                return  # Older schemas stay readable; Core owns migrations.
            if "CHECK" in (schema[0] or "").upper() and "summary_version_mismatch" not in schema[0]:
                # Keep existing diagnostics when a newer reader precedes Core migration.
                events = {key: count for key, count in events.items() if key[2] != "summary_version_mismatch"}
                if not events:
                    return
            now = int(time.time() * 1000)
            conn.executemany("""INSERT INTO document_source_mismatch_events
                (document_id,component,reason,expected_snapshot_id,observed_snapshot_id,occurrences,observed_at)
                VALUES (?,?,?,?,?,?,?)""", [key + (count, now) for key, count in events.items()])
    except sqlite3.Error:
        # Never weaken rejection or leak database/page errors on metrics failure.
        logger.warning("document_source_audit_write_failed occurrences=%s", sum(events.values()))
