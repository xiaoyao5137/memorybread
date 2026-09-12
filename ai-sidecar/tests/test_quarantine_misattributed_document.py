import sqlite3
from pathlib import Path

import pytest

from scripts.quarantine_misattributed_document import quarantine


def _seed(db):
    with sqlite3.connect(str(db)) as conn:
        conn.executescript("""
        CREATE TABLE bake_documents (
            id INTEGER PRIMARY KEY, source_url TEXT, full_content TEXT,
            creation_mode TEXT, deleted_at INTEGER, updated_at INTEGER
        );
        CREATE TABLE captures (
            id INTEGER PRIMARY KEY, ts INTEGER, url TEXT, webpage_title TEXT,
            win_title TEXT, ax_text TEXT
        );
        CREATE TABLE memory_favorites (
            resource_kind TEXT, resource_id INTEGER, created_at INTEGER, updated_at INTEGER
        );
        INSERT INTO bake_documents VALUES (
            974, 'https://example.com/aigc-assets/video-samples?assetId=1009',
            '# 灵机视频质量提升方案 - 讨论稿\n\n## 自动优化',
            'llm_bake', NULL, 100
        );
        INSERT INTO captures VALUES (
            1, 10, 'https://example.com/aigc-assets/video-samples?assetId=1009',
            '数据资产平台', '数据资产平台', '数据资产平台 视频样本数 8435'
        );
        INSERT INTO captures VALUES (
            2, 20, 'https://example.com/aigc-assets/video-samples?assetId=1009',
            '数据资产平台', '数据资产平台',
            '灵机视频质量提升方案 - 讨论稿 - 云文档 You need to enable JavaScript'
        );
        INSERT INTO memory_favorites VALUES ('document', 974, 50, 80);
        """)


def test_quarantine_requires_verified_cross_tab_conflict_and_is_reversible(tmp_path):
    db = tmp_path / "memory.db"
    _seed(db)

    preview = quarantine(db, 974, tmp_path)
    assert preview == {
        "document_id": 974,
        "conflicting_capture_ids": [2],
        "applied": False,
    }

    result = quarantine(db, 974, tmp_path, apply=True)
    backup = Path(result["backup"])
    assert result["applied"] is True
    assert (backup / "backup.json").exists()
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute(
            "SELECT deleted_at FROM bake_documents WHERE id=974"
        ).fetchone()[0]
        assert conn.execute("SELECT count(*) FROM memory_favorites").fetchone()[0] == 0
        conn.executescript((backup / "restore.sql").read_text(encoding="utf-8"))
        assert conn.execute(
            "SELECT deleted_at,updated_at FROM bake_documents WHERE id=974"
        ).fetchone() == (None, 100)


def test_quarantine_rejects_unproven_document(tmp_path):
    db = tmp_path / "memory.db"
    _seed(db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("DELETE FROM captures WHERE id=2")
    with pytest.raises(ValueError, match="no verified cross-tab source conflict"):
        quarantine(db, 974, tmp_path, apply=True)
