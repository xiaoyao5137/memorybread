import sqlite3
from pathlib import Path
import pytest
from scripts.quarantine_document_shells import quarantine


def test_quarantine_is_scoped_backed_up_and_reversible(tmp_path):
    db = tmp_path / 'memory.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('''
        CREATE TABLE bake_documents (id INTEGER PRIMARY KEY, full_content TEXT, deleted_at INTEGER, updated_at INTEGER);
        CREATE TABLE memory_favorites (resource_kind TEXT, resource_id INTEGER, created_at INTEGER, updated_at INTEGER);
        INSERT INTO bake_documents VALUES (1, '知识库首页目录 收藏 分享 编辑 全部暂停', NULL, 100);
        INSERT INTO bake_documents VALUES (2, '正常文档正文', NULL, 200);
        INSERT INTO memory_favorites VALUES ('document', 1, 50, 80);
        ''')
    assert quarantine(db, [1], tmp_path)['applied'] is False
    with pytest.raises(ValueError):
        quarantine(db, [1, 2], tmp_path, True)
    result = quarantine(db, [1], tmp_path, True)
    backup = Path(result['backup'])
    assert (backup / 'backup.json').exists()
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT deleted_at FROM bake_documents WHERE id=1').fetchone()[0]
        assert conn.execute('SELECT deleted_at FROM bake_documents WHERE id=2').fetchone()[0] is None
        conn.executescript((backup / 'restore.sql').read_text())
        assert conn.execute('SELECT deleted_at,updated_at FROM bake_documents WHERE id=1').fetchone() == (None, 100)
        assert conn.execute('SELECT count(*) FROM memory_favorites').fetchone()[0] == 1
