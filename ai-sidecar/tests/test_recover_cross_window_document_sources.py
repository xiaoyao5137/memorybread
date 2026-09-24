import importlib.util
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "recover_cross_window_document_sources.py"
SPEC = importlib.util.spec_from_file_location("recover_cross_window_document_sources", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _database(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE bake_documents (
            id INTEGER PRIMARY KEY, title TEXT, source_url TEXT, creation_mode TEXT,
            deleted_at INTEGER, source_memory_ids TEXT, source_episode_ids TEXT,
            source_capture_ids TEXT, updated_at INTEGER
        );
        CREATE TABLE timelines (
            id INTEGER PRIMARY KEY, capture_id INTEGER, start_time INTEGER,
            observed_at INTEGER, created_at_ms INTEGER
        );
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY, timeline_id INTEGER, url TEXT,
            webpage_title TEXT, win_title TEXT, ax_text TEXT, ocr_text TEXT
        );
        CREATE TABLE bake_artifact_audits (
            id INTEGER PRIMARY KEY, timeline_id INTEGER, artifact_kind TEXT,
            persist_status TEXT, artifact_id INTEGER
        );
        CREATE TABLE bake_document_source_fingerprints (
            document_id INTEGER, fingerprint TEXT, source_timeline_id INTEGER
        );
        CREATE TABLE bake_retry_state (
            timeline_id INTEGER PRIMARY KEY, failure_count INTEGER, last_error TEXT,
            last_failed_at_ms INTEGER, last_error_code TEXT, next_retry_at_ms INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO bake_documents VALUES (16,'错误文档','https://wrong.test/doc','llm_bake',NULL,'[]','[]','[]',0)"
    )
    for timeline_id in (1, 2, 3):
        conn.execute(
            "INSERT INTO timelines VALUES (?,?,1000,NULL,NULL)",
            (timeline_id, 100 + timeline_id),
        )
        conn.execute(
            "INSERT INTO captures VALUES (?,?,?,'错误文档','真实文档 - Google Chrome',NULL,?)",
            (100 + timeline_id, timeline_id, "https://wrong.test/doc", "正文。" * 100),
        )
        conn.execute(
            "INSERT INTO bake_artifact_audits VALUES (?,?, 'document','created',16)",
            (timeline_id, timeline_id),
        )
        conn.execute(
            "INSERT INTO bake_retry_state VALUES (?,2,'queued',1,'DOCUMENT_SOURCE_IDENTITY_MISMATCH',0)",
            (timeline_id,),
        )
    conn.execute(
        "INSERT INTO bake_documents VALUES (17,'已恢复','', 'llm_bake',NULL,'[\"1\"]','[]','[]',0)"
    )
    conn.execute(
        "INSERT INTO bake_document_source_fingerprints VALUES (17,'source-1',1)"
    )
    conn.execute(
        "INSERT INTO bake_artifact_audits VALUES (20,2,'document','rejected',NULL)"
    )
    conn.commit()
    return conn


def test_inspect_excludes_recovered_and_quality_rejected_sources(tmp_path):
    conn = _database(tmp_path / "memory.db")
    conn.row_factory = sqlite3.Row

    _, candidates, proven, recovered, rejected, pending = MODULE.inspect(
        conn, 16, 0, 2000
    )

    assert candidates == [1, 2, 3]
    assert proven == [1, 2, 3]
    assert recovered == [1]
    assert rejected == [2]
    assert pending == [3]


def test_recover_does_not_reset_pending_retry_and_clears_resolved(tmp_path):
    db_path = tmp_path / "memory.db"
    conn = _database(db_path)
    conn.close()

    report = MODULE.recover(db_path, 16, 0, 2000, tmp_path / "backups", apply=True)

    conn = sqlite3.connect(str(db_path))
    rows = dict(conn.execute(
        "SELECT timeline_id,failure_count FROM bake_retry_state ORDER BY timeline_id"
    ))
    assert rows == {3: 2}
    assert report["cleared_resolved_retry_count"] == 2
    assert report["pending_recovery_count"] == 1
