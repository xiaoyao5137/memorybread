import sqlite3
from pathlib import Path

import pytest

from embedding.document_source_audit import audit_source_mismatches, record_source_mismatch, record_unverified_heads


def prepare_db(path):
    migration = Path(__file__).parents[2] / 'core-engine/src/storage/migrations/121_document_source_mismatch_events.sql'
    with sqlite3.connect(path) as conn:
        conn.executescript(migration.read_text())
        conn.execute('CREATE TABLE source_test(value INTEGER)')


def test_old_audit_schema_keeps_existing_events_when_summary_reason_is_new(tmp_path):
    path = str(tmp_path / 'old-schema.db')
    prepare_db(path)
    class Reader:
        db_path = path
        @audit_source_mismatches('rag')
        def read(self):
            record_source_mismatch(1, 'summary_version_mismatch', 7)
            record_source_mismatch(1, 'head_invalid', 7)
    Reader().read()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT reason,occurrences FROM document_source_mismatch_events').fetchall() == [('head_invalid', 1)]


def test_audit_flushes_after_transaction_and_preserves_rejection(tmp_path):
    path = str(tmp_path / 'audit.db')
    prepare_db(path)

    class Writer:
        db_path = path

        @audit_source_mismatches('vector_write')
        def reject(self):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('INSERT INTO source_test VALUES(1)')
                record_source_mismatch(953, 'snapshot_mismatch', 61, 60)
                record_source_mismatch(953, 'snapshot_mismatch', 61, 60)
                record_source_mismatch(953, 'https://private.invalid/secret', 61, 'secret')
                return False

    assert Writer().reject() is False
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM source_test').fetchone()[0] == 1
        assert conn.execute('SELECT document_id,component,reason,expected_snapshot_id,observed_snapshot_id,occurrences FROM document_source_mismatch_events').fetchall() == [
            (953, 'vector_write', 'snapshot_mismatch', 61, 60, 2)]


def test_audit_does_not_replace_caller_exception_or_persist_invalid_ids(tmp_path):
    path = str(tmp_path / 'audit.db')
    prepare_db(path)

    class Reader:
        db_path = path

        @audit_source_mismatches('rag')
        def reject(self):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('INSERT INTO source_test VALUES(1)')
                record_source_mismatch(953, 'head_invalid', 'private text', True)
                raise ValueError('caller failure')

    with pytest.raises(ValueError, match='caller failure'):
        Reader().reject()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM source_test').fetchone()[0] == 0
        assert conn.execute('SELECT expected_snapshot_id,observed_snapshot_id,occurrences FROM document_source_mismatch_events').fetchall() == [(None, None, 1)]


def test_legacy_schema_stays_unchanged_and_lock_failure_does_not_allow_result(tmp_path):
    path = str(tmp_path / 'legacy.db')

    class Reader:
        db_path = path

        @audit_source_mismatches('rag')
        def reject(self):
            record_source_mismatch(1, 'head_invalid')
            return []

    assert Reader().reject() == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    prepare_db(path)
    with sqlite3.connect(path) as locker:
        locker.execute('BEGIN EXCLUSIVE')
        assert Reader().reject() == []


def test_audit_rejects_unknown_component():
    with pytest.raises(ValueError):
        audit_source_mismatches('private-url')


def test_creation_counts_only_evaluated_rejected_heads(tmp_path):
    path = str(tmp_path / 'creation.db')
    prepare_db(path)
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER);'
                          'INSERT INTO bake_document_source_heads VALUES(1,7),(2,8),(3,9);')

    class Reader:
        db_path = path

        @audit_source_mismatches('creation')
        def evaluate(self):
            with sqlite3.connect(self.db_path) as conn:
                conn.execute('BEGIN')
                record_unverified_heads(conn, [
                    {'id': 1, 'source_snapshot_id': None},
                    {'id': 2, 'source_snapshot_id': 8},
                    {'id': 4, 'source_snapshot_id': None},
                ])

    Reader().evaluate()
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT document_id,component,reason,occurrences FROM document_source_mismatch_events').fetchall() == [(1, 'creation', 'head_invalid', 1)]
