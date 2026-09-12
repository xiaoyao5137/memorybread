#!/usr/bin/env python3
"""Audit metadata-only document skips; explicitly enqueue selected sources.

Never changes capture timestamps, the global watermark, document body, or history.
Python 3.9 compatible. Backups include the exact retry rows replaced by this tool.
"""
import argparse
import json
import sqlite3
import time
from pathlib import Path


def replay(db_path, document_id, apply=False):
    conn = sqlite3.connect('file:' + str(Path(db_path).resolve()) + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    doc = conn.execute('SELECT id, source_memory_ids, source_episode_ids FROM bake_documents WHERE id=? AND deleted_at IS NULL', (document_id,)).fetchone()
    if doc is None:
        raise ValueError('active document not found')
    members = sorted(set(str(x) for name in ('source_memory_ids', 'source_episode_ids') for x in json.loads(doc[name] or '[]')))
    candidates = []
    for member in members:
        rows = conn.execute("SELECT id FROM bake_candidate_audits WHERE timeline_id=? AND persist_reason IN ('existing_document_url_linked','document_url_already_queued')", (member,)).fetchall()
        applied = conn.execute('SELECT 1 FROM bake_document_source_fingerprints WHERE document_id=? AND source_timeline_id=?', (document_id, member)).fetchone()
        if rows and not applied:
            candidates.append(int(member))
    report = {'document_id': document_id, 'timeline_ids': candidates, 'applied': False}
    has_source_queue = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='bake_document_refresh_observations'").fetchone())
    report['lane'] = 'source_refresh' if has_source_queue else 'bake_retry'
    if apply and candidates:
        conn.execute('BEGIN IMMEDIATE')
        prior = [dict(row) for tid in candidates for row in conn.execute('SELECT * FROM bake_retry_state WHERE timeline_id=?', (tid,))]
        prior_observations = ([dict(row) for row in conn.execute(
            'SELECT * FROM bake_document_refresh_observations WHERE document_id=?', (document_id,))]
            if has_source_queue else [])
        backup_dir = Path(db_path).resolve().parent / 'repair-backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / ('document-update-replay-%s-%s.json' % (document_id, time.time_ns()))
        backup.write_text(json.dumps({'report': report, 'prior_retry_rows': prior,
                                     'prior_observations': prior_observations}, ensure_ascii=False, indent=2), encoding='utf-8')
        now = int(time.time() * 1000)
        for tid in candidates:
            if has_source_queue:
                # This identifies a historical observation to check, not source
                # text claimed to have been applied to the document body.
                conn.execute("""INSERT OR IGNORE INTO bake_document_refresh_observations
                    (document_id,fingerprint,source_timeline_id,created_at,updated_at)
                    VALUES (?,?,?,?,?)""", (document_id, 'historical-replay:%s:%s' % (document_id, tid), tid, now, now))
                continue
            conn.execute("""INSERT INTO bake_retry_state (timeline_id, failure_count, last_error, last_failed_at_ms, last_error_code, next_retry_at_ms)
                VALUES (?,1,'source metadata linked but body not applied',?,'BAKE_DOCUMENT_MERGE_PENDING',0)
                ON CONFLICT(timeline_id) DO UPDATE SET failure_count=1,
                last_error=excluded.last_error,last_failed_at_ms=excluded.last_failed_at_ms,
                last_error_code=excluded.last_error_code,next_retry_at_ms=0""", (tid, now))
        conn.commit()
        report.update(applied=True, backup=str(backup))
    conn.close()
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    parser.add_argument('--document-id', type=int, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(replay(args.db, args.document_id, args.apply), ensure_ascii=False))


if __name__ == '__main__':
    main()
