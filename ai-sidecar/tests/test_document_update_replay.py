import importlib.util
import json
import sqlite3
import pytest
from pathlib import Path

spec = importlib.util.spec_from_file_location('document_update_replay', Path(__file__).parents[1] / 'scripts' / 'replay_document_updates.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_source_index_migration_invalidates_only_superseded_versions():
    migration = (Path(__file__).parents[2] / 'core-engine' / 'src' / 'storage'
                 / 'migrations' / '116_document_source_index_invalidation.sql').read_text()
    with sqlite3.connect(':memory:') as conn:
        conn.executescript('''
            CREATE TABLE artifact_vector_index(id INTEGER,document_id INTEGER,qdrant_point_id TEXT,indexed_at INTEGER);
            CREATE TABLE bake_document_source_heads(document_id INTEGER,applied_at INTEGER);
            CREATE TABLE vector_deletion_queue(qdrant_point_id TEXT PRIMARY KEY,source_type TEXT,reason TEXT,enqueued_at INTEGER);
            INSERT INTO bake_document_source_heads VALUES(1,100);
            INSERT INTO artifact_vector_index VALUES(1,1,'stale',99),(2,1,'current',100),(3,2,'unrelated',1);
        ''')
        conn.executescript(migration)
        conn.executescript(migration)
        assert conn.execute('SELECT qdrant_point_id FROM artifact_vector_index ORDER BY id').fetchall() == [('current',), ('unrelated',)]
        assert conn.execute('SELECT qdrant_point_id,reason FROM vector_deletion_queue').fetchall() == [('stale', 'document_source_replaced')]


def test_replay_keeps_document_body_and_watermark(tmp_path):
    db = tmp_path / 'test.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE bake_documents(id INTEGER,source_memory_ids TEXT,source_episode_ids TEXT,deleted_at INTEGER,full_content TEXT);
            CREATE TABLE bake_candidate_audits(id INTEGER,timeline_id INTEGER,persist_reason TEXT);
            CREATE TABLE bake_document_source_fingerprints(document_id INTEGER,source_timeline_id INTEGER);
            CREATE TABLE bake_retry_state(timeline_id INTEGER PRIMARY KEY,failure_count INTEGER,last_error TEXT,last_failed_at_ms INTEGER,last_error_code TEXT,next_retry_at_ms INTEGER);
            CREATE TABLE bake_watermarks(last_processed_ts INTEGER);
            INSERT INTO bake_documents VALUES(1,'[10,11]','[]',NULL,'original');
            INSERT INTO bake_candidate_audits VALUES(1,10,'existing_document_url_linked'),(2,11,'existing_document_url_linked');
            INSERT INTO bake_document_source_fingerprints VALUES(1,10);
            INSERT INTO bake_watermarks VALUES(12345);
        ''')
    report = module.replay(db, 1)
    assert report['timeline_ids'] == [11]
    assert not report['applied']
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM bake_retry_state').fetchone()[0] == 0
    result = module.replay(db, 1, True)
    assert result['applied']
    assert json.loads(Path(result['backup']).read_text())['prior_retry_rows'] == []
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT full_content FROM bake_documents').fetchone()[0] == 'original'
        assert conn.execute('SELECT last_processed_ts FROM bake_watermarks').fetchone()[0] == 12345
        assert conn.execute('SELECT timeline_id,last_error_code FROM bake_retry_state').fetchone() == (11, 'BAKE_DOCUMENT_MERGE_PENDING')
        conn.execute('CREATE TABLE bake_document_refresh_observations(document_id INTEGER,fingerprint TEXT UNIQUE,source_timeline_id INTEGER,created_at INTEGER,updated_at INTEGER)')
    queued = module.replay(db, 1, True)
    assert queued['lane'] == 'source_refresh'
    module.replay(db, 1, True)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT document_id,source_timeline_id FROM bake_document_refresh_observations').fetchall() == [(1, 11)]
        assert conn.execute('SELECT full_content FROM bake_documents').fetchone()[0] == 'original'


@pytest.mark.parametrize('legacy_schema', [False, True])
def test_body_restore_preserves_new_source_links_and_requires_apply(tmp_path, legacy_schema):
    restore_spec = importlib.util.spec_from_file_location('restore_body', Path(__file__).parents[1] / 'scripts' / 'restore_document_body_version.py')
    restore_module = importlib.util.module_from_spec(restore_spec)
    restore_spec.loader.exec_module(restore_module)
    db = tmp_path / 'restore.db'
    old = {name: 'old' for name in restore_module.BODY_FIELDS}
    fields = tuple(name for name in restore_module.BODY_FIELDS
                   if not legacy_schema or name not in ('diagram_code', 'image_assets', 'language', 'match_score', 'match_level'))
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE bake_documents(id INTEGER,deleted_at INTEGER,source_capture_ids TEXT,updated_at INTEGER,last_refresh_status TEXT,last_refresh_completeness TEXT,' + ','.join(name+' TEXT' for name in fields) + ')')
        conn.execute("INSERT INTO bake_documents(id,source_capture_ids,full_content) VALUES(1,'[22]','new')")
        conn.execute('ALTER TABLE bake_documents ADD COLUMN summary_source_snapshot_id INTEGER')
        conn.execute('ALTER TABLE bake_documents ADD COLUMN summary_generation_version TEXT')
        conn.execute("UPDATE bake_documents SET summary_source_snapshot_id=2,summary_generation_version='new-version'")
        conn.execute('CREATE TABLE bake_document_body_versions(id INTEGER,document_id INTEGER,record_json TEXT)')
        conn.execute('INSERT INTO bake_document_body_versions VALUES(1,1,?)', (json.dumps(old),))
        conn.execute('CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER)')
        conn.execute('INSERT INTO bake_document_source_heads VALUES(1,2)')
        conn.execute('CREATE TABLE artifact_vector_index(document_id INTEGER,qdrant_point_id TEXT)')
        conn.execute("INSERT INTO artifact_vector_index VALUES(1,'replaced'),(2,'unrelated')")
        conn.execute('CREATE TABLE vector_deletion_queue(qdrant_point_id TEXT PRIMARY KEY,source_type TEXT,reason TEXT,enqueued_at INTEGER)')
    assert not restore_module.restore(db, 1)['applied']
    result = restore_module.restore(db, 1, True)
    assert result['applied']
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT full_content,source_capture_ids,last_refresh_completeness FROM bake_documents').fetchone() == ('old','[22]','unverified')
        assert conn.execute('SELECT ' + ','.join(fields) + ' FROM bake_documents').fetchone() == tuple(old[name] for name in fields)
        assert conn.execute('SELECT summary_source_snapshot_id,summary_generation_version FROM bake_documents').fetchone() == (None,None)
        assert conn.execute('SELECT COUNT(*) FROM bake_document_source_heads').fetchone()[0] == 0
        assert conn.execute('SELECT qdrant_point_id FROM artifact_vector_index').fetchall() == [('unrelated',)]
        assert conn.execute('SELECT qdrant_point_id,source_type,reason FROM vector_deletion_queue').fetchall() == [('replaced','document','document_body_restored')]
    assert json.loads(Path(result['backup']).read_text())['full_content'] == 'new'


@pytest.mark.parametrize('refresh_config,capture_enabled', [
    ({}, 'true'),
    ({'enabled': False, 'source_writes_enabled': False}, 'true'),
    ({'rollout_document_ids': []}, 'false'),
])
def test_restored_shell_stays_isolated_across_document_consumers(tmp_path, refresh_config, capture_enabled):
    from creation.service import CreationService, CreationOptions
    from embedding.document_chunks import build_bake_document_snapshot
    from rag.retriever import KnowledgeFts5Retriever, RetrievedChunk
    restore_spec = importlib.util.spec_from_file_location('restore_consumers', Path(__file__).parents[1] / 'scripts/restore_document_body_version.py')
    restore_module = importlib.util.module_from_spec(restore_spec)
    restore_spec.loader.exec_module(restore_module)
    db = tmp_path / 'consumers.db'
    shell = '知识库 首页 目录 收藏 分享 编辑 全部暂停'
    old = {'title': '缓存策略', 'full_content': shell,
           'summary': '缓存策略的详细业务实现步骤。' * 100,
           'sections_json': json.dumps([{'content': '缓存更新时需要校验版本，成功后再切换读取来源并重建索引。' * 100}])}
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE bake_documents(id INTEGER PRIMARY KEY,title TEXT,full_content TEXT,summary TEXT,
                sections_json TEXT,source_url TEXT,deleted_at INTEGER,updated_at INTEGER,
                last_refresh_status TEXT,last_refresh_completeness TEXT,doc_type TEXT,
                source_memory_ids TEXT,linked_knowledge_ids TEXT,source_capture_ids TEXT,
                style_phrases TEXT,prompt_hint TEXT,usage_count INTEGER,review_status TEXT);
            INSERT INTO bake_documents VALUES(1,'缓存策略','当前可靠正文',NULL,'[]','https://docs.example.com/document/1',
                NULL,100,'updated','complete','技术文档','[]','[]','[22]','[]','',0,'auto_created');
            CREATE TABLE bake_document_body_versions(id INTEGER,document_id INTEGER,record_json TEXT);
            CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER);
            INSERT INTO bake_document_source_heads VALUES(1,2);
            CREATE TABLE artifact_vector_index(document_id INTEGER,qdrant_point_id TEXT);
            INSERT INTO artifact_vector_index VALUES(1,'stale-point');
            CREATE TABLE vector_deletion_queue(qdrant_point_id TEXT PRIMARY KEY,source_type TEXT,reason TEXT,enqueued_at INTEGER);
            CREATE TABLE user_preferences(key TEXT PRIMARY KEY,value TEXT);
        ''')
        conn.executemany('INSERT INTO user_preferences(key,value) VALUES(?,?)', [
            ('runtime.document_source_refresh', json.dumps(refresh_config)),
            ('runtime.capture_enabled', capture_enabled),
        ])
        conn.execute('INSERT INTO bake_document_body_versions VALUES(1,1,?)', (json.dumps(old),))
    result = restore_module.restore(db, 1, True)
    assert result['applied']
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute('SELECT * FROM bake_documents WHERE id=1').fetchone())
        assert row['full_content'] == shell and row['source_capture_ids'] == '[22]'
        assert conn.execute('SELECT COUNT(*) FROM bake_document_source_heads').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM artifact_vector_index').fetchone()[0] == 0
        assert conn.execute('SELECT qdrant_point_id FROM vector_deletion_queue').fetchone()[0] == 'stale-point'
        assert json.loads(conn.execute("SELECT value FROM user_preferences WHERE key='runtime.document_source_refresh'").fetchone()[0]) == refresh_config
    assert json.loads(Path(result['backup']).read_text())['full_content'] == '当前可靠正文'
    assert build_bake_document_snapshot(row) is None
    stale = RetrievedChunk(capture_id=22, text='旧错误摘要', score=1.0, source='vector',
        doc_key='document_url:https://docs.example.com/document/1',
        metadata={'source_type': 'document', 'url': 'https://docs.example.com/document/1'})
    assert KnowledgeFts5Retriever(str(db)).materialize_documents([stale], '缓存策略') == []
    service = CreationService.__new__(CreationService)
    service.db_path = str(db)
    service.enable_vector_recall = False
    parsed = {'keywords': ['缓存策略']}
    assert service.retrieve_references('缓存策略', parsed, CreationOptions()) == []
    assert parsed['retrieval_diagnostics']['filter_counts']['document_shell'] == 1


@pytest.mark.parametrize('invalid', [None, 'owner', 'url', 'body', 'hash', 'partial', 'identity', 'truncated', 'shell'])
def test_explicit_source_restore_validates_before_writing(tmp_path, invalid):
    import hashlib
    from embedding.document_source import source_snapshot_select, source_summary_select
    restore_spec = importlib.util.spec_from_file_location('restore_source', Path(__file__).parents[1] / 'scripts/restore_document_body_version.py')
    restore_module = importlib.util.module_from_spec(restore_spec)
    restore_spec.loader.exec_module(restore_module)
    db = tmp_path / 'source.db'
    body = '页面加载失败' if invalid == 'shell' else '历史可靠正文：缓存更新步骤。'
    digest = hashlib.sha256(body.encode()).hexdigest()
    archived = {'full_content': body, 'summary': '旧摘要', 'summary_source_snapshot_id': 7,
                'summary_generation_version': 'document-summary.v1'}
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE bake_documents(id INTEGER PRIMARY KEY,full_content TEXT,summary TEXT,source_url TEXT,deleted_at INTEGER,
                updated_at INTEGER,last_refresh_status TEXT,last_refresh_completeness TEXT,content_hash TEXT,
                summary_source_snapshot_id INTEGER,summary_generation_version TEXT);
            INSERT INTO bake_documents VALUES(1,'当前正文','当前摘要','https://docs.example.com/document/1',NULL,100,'updated','complete','new',8,'new');
            CREATE TABLE bake_document_body_versions(id INTEGER,document_id INTEGER,record_json TEXT);
            CREATE TABLE bake_document_source_heads(document_id INTEGER PRIMARY KEY,snapshot_id INTEGER,applied_at INTEGER);
            INSERT INTO bake_document_source_heads VALUES(1,8,100);
            CREATE TABLE bake_document_source_snapshots(id INTEGER,document_id INTEGER,source_url TEXT,content_text TEXT,
                content_hash TEXT,completeness_status TEXT,identity_match INTEGER,truncated INTEGER);
        ''')
        conn.execute('INSERT INTO bake_document_body_versions VALUES(1,1,?)', (json.dumps(archived),))
        conn.execute('INSERT INTO bake_document_source_snapshots VALUES(7,?,?,?,?,?,?,?)', (
            2 if invalid == 'owner' else 1,
            'https://docs.example.com/document/2' if invalid == 'url' else 'https://docs.example.com/document/1',
            '错配正文' if invalid == 'body' else body, 'wrong' if invalid == 'hash' else digest,
            'partial' if invalid == 'partial' else 'complete', 0 if invalid == 'identity' else 1,
            1 if invalid == 'truncated' else 0))
    before = db.read_bytes()
    if invalid:
        for apply in [False, True]:
            with pytest.raises(ValueError, match='does not verify'):
                restore_module.restore(db, 1, apply, 7)
        assert db.read_bytes() == before
        assert not (tmp_path / 'repair-backups').exists()
        return
    assert restore_module.restore(db, 1, False, 7)['source_snapshot_id'] == 7
    assert db.read_bytes() == before
    # Fail after body/head/summary updates: index cleanup must be part of the
    # same transaction, otherwise rollback can expose mixed source versions.
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE artifact_vector_index(document_id INTEGER,qdrant_point_id TEXT);
            INSERT INTO artifact_vector_index VALUES(1,'current-point');
            CREATE TABLE vector_deletion_queue(qdrant_point_id TEXT PRIMARY KEY,source_type TEXT,reason TEXT,enqueued_at INTEGER);
            CREATE TRIGGER reject_cleanup BEFORE INSERT ON vector_deletion_queue
            BEGIN SELECT RAISE(ABORT,'isolated cleanup failure'); END;
        ''')
        before_rows = list(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError, match='isolated cleanup failure'):
        restore_module.restore(db, 1, True, 7)
    with sqlite3.connect(db) as conn:
        assert list(conn.iterdump()) == before_rows
        conn.execute('DROP TRIGGER reject_cleanup')
    result = restore_module.restore(db, 1, True, 7)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT snapshot_id FROM bake_document_source_heads').fetchone() == (7,)
        assert conn.execute('SELECT content_hash,last_refresh_completeness FROM bake_documents').fetchone() == (digest, 'complete')
        projection = 'SELECT ' + source_snapshot_select(conn) + ',' + source_summary_select(conn) + ' FROM bake_documents d'
        assert conn.execute(projection).fetchone() == (7, '旧摘要')
        assert conn.execute('SELECT COUNT(*) FROM artifact_vector_index').fetchone() == (0,)
        assert conn.execute('SELECT qdrant_point_id FROM vector_deletion_queue').fetchone() == ('current-point',)
    backup = json.loads(Path(result['backup']).read_text())
    assert backup['_source_head'] == {'document_id': 1, 'snapshot_id': 8, 'applied_at': 100}
    assert backup['full_content'] == '当前正文'
    # A matching body cannot retroactively authorize an unbound or differently
    # bound archived summary, including JSON true masquerading as an integer.
    for binding in [None, 8, True]:
        archived['summary_source_snapshot_id'] = binding
        with sqlite3.connect(db) as conn:
            conn.execute('UPDATE bake_document_body_versions SET record_json=?', (json.dumps(archived),))
        restore_module.restore(db, 1, True, 7)
        with sqlite3.connect(db) as conn:
            projection = 'SELECT ' + source_snapshot_select(conn) + ',' + source_summary_select(conn) + ' FROM bake_documents d'
            assert conn.execute(projection).fetchone() == (7, None)


def test_restore_deletion_outbox_removes_real_isolated_qdrant_point(tmp_path):
    from embedding.vector_storage import VectorStorage
    from qdrant_client.models import PointStruct
    from qdrant_client import QdrantClient
    restore_spec = importlib.util.spec_from_file_location('restore_qdrant', Path(__file__).parents[1] / 'scripts/restore_document_body_version.py')
    restore_module = importlib.util.module_from_spec(restore_spec)
    restore_spec.loader.exec_module(restore_module)
    db = tmp_path / 'rollback.db'
    point = '11111111-1111-4111-8111-111111111111'
    untouched = '22222222-2222-4222-8222-222222222222'
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE bake_documents(id INTEGER PRIMARY KEY,full_content TEXT,summary TEXT,
                deleted_at INTEGER,updated_at INTEGER,last_refresh_status TEXT,last_refresh_completeness TEXT);
            INSERT INTO bake_documents VALUES(1,'new body','new summary',NULL,1,'updated','complete');
            CREATE TABLE bake_document_body_versions(id INTEGER,document_id INTEGER,record_json TEXT);
            CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER);
            INSERT INTO bake_document_source_heads VALUES(1,8);
            CREATE TABLE artifact_vector_index(document_id INTEGER,qdrant_point_id TEXT);
            CREATE TABLE vector_deletion_queue(qdrant_point_id TEXT PRIMARY KEY,source_type TEXT,
                reason TEXT,enqueued_at INTEGER,attempt_count INTEGER DEFAULT 0,
                next_attempt_at INTEGER DEFAULT 0,last_error TEXT);
        ''')
        conn.execute('INSERT INTO bake_document_body_versions VALUES(1,1,?)', (json.dumps({'full_content': 'old body'}),))
        conn.executemany('INSERT INTO artifact_vector_index VALUES(?,?)', [(1,point),(2,untouched)])
    directory = tmp_path / 'isolated-qdrant'
    storage = VectorStorage(db_path=str(db), qdrant_path=str(directory))
    client = storage._get_qdrant_client()
    assert client is not None and directory.exists()
    try:
        client.upsert(collection_name=storage._collection_name, points=[
            PointStruct(id=pid, vector=[1.0] + [0.0] * 511, payload={'document_id': owner})
            for pid,owner in [(point,1),(untouched,2)]
        ])
        assert restore_module.restore(db, 1, True)['applied']
        assert len(client.retrieve(storage._collection_name, ids=[point])) == 1
        assert storage.drain_deletion_queue() == {'selected_count': 1, 'deleted_count': 1}
        assert client.retrieve(storage._collection_name, ids=[point]) == []
        assert len(client.retrieve(storage._collection_name, ids=[untouched])) == 1
        assert storage.drain_deletion_queue() == {'selected_count': 0, 'deleted_count': 0}
        with sqlite3.connect(db) as conn:
            assert conn.execute('SELECT COUNT(*) FROM vector_deletion_queue').fetchone() == (0,)
            assert conn.execute('SELECT qdrant_point_id FROM artifact_vector_index').fetchall() == [(untouched,)]
    finally:
        client.close()
    reopened = QdrantClient(path=str(directory))
    try:
        assert reopened.retrieve(storage._collection_name, ids=[point]) == []
        assert len(reopened.retrieve(storage._collection_name, ids=[untouched])) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize('change', [None, 'backup', 'document', 'database', 'receipt_write'])
def test_restore_receipt_undo_is_checked_and_preserves_history(tmp_path, change, monkeypatch):
    import hashlib
    import subprocess
    restore_spec = importlib.util.spec_from_file_location('restore_receipt', Path(__file__).parents[1] / 'scripts/restore_document_body_version.py')
    restore_module = importlib.util.module_from_spec(restore_spec)
    restore_spec.loader.exec_module(restore_module)
    db = tmp_path / 'receipt.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE bake_documents(id INTEGER PRIMARY KEY,full_content TEXT,summary TEXT,
                deleted_at INTEGER,updated_at INTEGER,last_refresh_status TEXT,last_refresh_completeness TEXT);
            INSERT INTO bake_documents VALUES(1,'before','before summary',NULL,1,'historical_only','unverified');
            CREATE TABLE bake_document_body_versions(id INTEGER,document_id INTEGER,record_json TEXT);
            CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER);
        ''')
        conn.execute('INSERT INTO bake_document_body_versions VALUES(1,1,?)', (json.dumps({'full_content': 'restored'}),))
    if change == 'receipt_write':
        write = restore_module._write_private_json
        def fail_receipt(path, value):
            if path.name.endswith('.receipt.json'):
                raise OSError('isolated receipt write failure')
            return write(path, value)
        monkeypatch.setattr(restore_module, '_write_private_json', fail_receipt)
        with sqlite3.connect(db) as conn:
            before_dump = list(conn.iterdump())
        with pytest.raises(OSError, match='receipt write failure'):
            restore_module.restore(db, 1, True)
        with sqlite3.connect(db) as conn:
            assert list(conn.iterdump()) == before_dump
        assert not list((tmp_path / 'repair-backups').glob('*.receipt.json'))
        return
    result = restore_module.restore(db, 1, True)
    receipt_path = Path(result['manifest'])
    receipt = json.loads(receipt_path.read_text())
    backup = Path(result['backup'])
    assert receipt['backup_sha256'] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600
    if change == 'backup':
        backup.write_text('{}')
    elif change == 'document':
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE bake_documents SET full_content='later edit' WHERE id=1")
    elif change == 'database':
        receipt['database'] = str(tmp_path / 'other.db')
        receipt_path.write_text(json.dumps(receipt))
    before = db.read_bytes()
    if change:
        for apply in [False, True]:
            with pytest.raises(ValueError):
                restore_module.restore(db, apply=apply, undo_manifest=receipt_path)
        assert db.read_bytes() == before
        return
    assert not restore_module.restore(db, undo_manifest=receipt_path)['applied']
    assert db.read_bytes() == before
    completed = subprocess.run(receipt['reverse_argv'], check=True, capture_output=True, text=True)
    undo = json.loads(completed.stdout)
    assert undo['applied']
    assert json.loads(Path(undo['manifest']).read_text())['undo_of'] == str(receipt_path)
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT full_content,summary FROM bake_documents').fetchone() == ('before','before summary')
        assert conn.execute('SELECT COUNT(*) FROM bake_document_body_versions').fetchone() == (1,)
    with pytest.raises(ValueError, match='changed since restore'):
        restore_module.restore(db, apply=True, undo_manifest=receipt_path)
