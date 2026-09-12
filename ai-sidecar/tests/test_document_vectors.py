from __future__ import annotations

import asyncio
import json
import sqlite3

from background_processor import BackgroundProcessor
from embedding.base import EmbeddingVector
from embedding.document_chunks import (
    build_bake_document_snapshot,
    build_document_snapshot,
    canonicalize_document_url,
    chunk_document,
    estimate_tokens,
)
from embedding.vector_storage import VectorStorage


def _create_vector_schema(path: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE captures (id INTEGER PRIMARY KEY)")
        conn.execute(
            """
            CREATE TABLE vector_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                capture_id INTEGER NOT NULL,
                qdrant_point_id TEXT NOT NULL UNIQUE,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                chunk_text TEXT NOT NULL,
                model_name TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                doc_key TEXT,
                source_type TEXT NOT NULL DEFAULT 'capture',
                knowledge_id INTEGER,
                time INTEGER,
                start_time INTEGER,
                end_time INTEGER,
                observed_at INTEGER,
                event_time_start INTEGER,
                event_time_end INTEGER,
                history_view INTEGER NOT NULL DEFAULT 0,
                content_origin TEXT,
                activity_type TEXT,
                is_self_generated INTEGER NOT NULL DEFAULT 0,
                evidence_strength TEXT,
                app_name TEXT,
                win_title TEXT,
                category TEXT,
                user_verified INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("INSERT INTO captures (id) VALUES (7)")


def _create_durable_vector_schema(path: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE bake_documents (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                doc_type TEXT NOT NULL DEFAULT 'article',
                summary TEXT,
                full_content TEXT,
                sections_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT,
                deleted_at INTEGER,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE artifact_vector_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id INTEGER NOT NULL,
                qdrant_point_id TEXT NOT NULL UNIQUE,
                doc_key TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                model_name TEXT NOT NULL,
                indexed_at INTEGER NOT NULL,
                UNIQUE(document_id, content_hash, chunk_index)
            );
            CREATE TABLE vector_deletion_queue (
                qdrant_point_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                enqueued_at INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at INTEGER NOT NULL DEFAULT 0
            );
            CREATE TRIGGER artifact_vector_queue_delete
            AFTER DELETE ON artifact_vector_index
            BEGIN
                INSERT OR IGNORE INTO vector_deletion_queue (
                    qdrant_point_id, source_type, reason, enqueued_at
                )
                VALUES (
                    old.qdrant_point_id,
                    'document',
                    'artifact_vector_replaced_or_deleted',
                    1
                );
            END;
            """
        )


class _FakeQdrant:
    def __init__(self) -> None:
        self.upserts = []
        self.deletes = []
        self.point_ids = set()

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)
        self.point_ids.update(str(point.id) for point in kwargs["points"])

    def delete(self, **kwargs):
        self.deletes.append(kwargs)
        self.point_ids.difference_update(
            str(point_id) for point_id in kwargs["points_selector"].points
        )

    def retrieve(self, **kwargs):
        class _Point:
            def __init__(self, point_id):
                self.id = point_id

        return [
            _Point(point_id)
            for point_id in kwargs["ids"]
            if str(point_id) in self.point_ids
        ]

    def scroll(self, **_kwargs):
        class _Point:
            def __init__(self, point_id):
                self.id = point_id

        return [_Point(point_id) for point_id in sorted(self.point_ids)], None


def test_document_chunking_keeps_content_after_old_500_character_cutoff() -> None:
    body = (
        "第一章：背景\n\n"
        + "这是背景说明。" * 45
        + "\n\n第二章：潮汐\n\n"
        + "潮汐特性用于控制后台任务的启动和并发水位。" * 40
    )
    chunks = chunk_document(body, title="系统调度方案")

    assert len(chunks) >= 2
    assert any("潮汐特性" in chunk for chunk in chunks[1:])
    assert all(estimate_tokens(chunk) <= 500 for chunk in chunks)


def test_url_identity_v2_shared_cases():
    import json
    from pathlib import Path
    from embedding.document_chunks import _canonicalize_url
    cases = json.loads((Path(__file__).resolve().parents[2] / "shared/document-quality/url-identity-v2-cases.json").read_text())
    for case in cases:
        left, right = _canonicalize_url(case["left"]), _canonicalize_url(case["right"])
        assert left and right
        assert (left == right) == case["equal"], case


def test_document_identity_preserves_resource_case_scheme_and_parameters():
    base = "https://docs.example.com/d/home/ABC123"
    assert canonicalize_document_url(base) == canonicalize_document_url(base + "?ro=false#comment")
    for other in (base.replace("ABC123", "abc123"), base.replace("https:", "http:"),
                  base + "?tenant=other", base + "/"):
        assert canonicalize_document_url(base) != canonicalize_document_url(other)


def test_document_snapshot_uses_canonical_url_and_full_ax_text() -> None:
    capture = {
        "id": 9,
        "url": "https://docs.example.com/k/home/sample-document?ro=false#section",
        "window_title": "调度文档",
        "ax_text": "前言。" * 120 + "潮汐特性在正文后部。",
        "ocr_text": "短 OCR",
    }
    snapshot = build_document_snapshot(capture)

    assert snapshot is not None
    assert snapshot.canonical_url == "https://docs.example.com/k/home/sample-document"
    assert snapshot.doc_key == f"document_url:{snapshot.canonical_url}"
    assert "潮汐特性" in snapshot.body
    assert canonicalize_document_url(capture["url"]) == snapshot.canonical_url


def test_document_vector_storage_is_idempotent_and_replaces_old_version(tmp_path, monkeypatch) -> None:
    db_path = str(tmp_path / "vectors.db")
    _create_vector_schema(db_path)
    storage = VectorStorage(db_path=db_path)
    qdrant = _FakeQdrant()
    monkeypatch.setattr(storage, "_get_qdrant_client", lambda: qdrant)
    metadata = {
        "doc_key": "document_url:https://docs.example.com/k/home/sample-document",
        "content_hash": "version-one",
        "url": "https://docs.example.com/k/home/sample-document",
        "title": "调度文档",
        "ts": 1234,
    }

    assert storage.store_document_vectors(
        7,
        ["第一块", "第二块包含潮汐特性"],
        [[0.1, 0.2], [0.2, 0.3]],
        metadata,
    )
    assert storage.store_document_vectors(
        7,
        ["第一块", "第二块包含潮汐特性"],
        [[0.1, 0.2], [0.2, 0.3]],
        metadata,
    )
    assert len(qdrant.upserts) == 1

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT chunk_index, chunk_text, source_type, doc_key FROM vector_index ORDER BY chunk_index"
        ).fetchall()
    assert rows == [
        (0, "第一块", "document", metadata["doc_key"]),
        (1, "第二块包含潮汐特性", "document", metadata["doc_key"]),
    ]

    changed = {**metadata, "content_hash": "version-two"}
    assert storage.store_document_vectors(
        7,
        ["新版本包含潮汐特性"],
        [[0.3, 0.4]],
        changed,
    )
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT chunk_index, chunk_text FROM vector_index ORDER BY chunk_index"
        ).fetchall()
    assert rows == [(0, "新版本包含潮汐特性")]
    assert len(qdrant.upserts) == 2
    assert len(qdrant.deletes) == 1


def test_document_version_exists_detects_embedding_model_change(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = str(tmp_path / "model-change.db")
    _create_vector_schema(db_path)
    storage = VectorStorage(db_path=db_path)
    monkeypatch.setattr(storage, "_get_qdrant_client", lambda: _FakeQdrant())
    metadata = {
        "doc_key": "document_url:https://docs.example.com/model-change",
        "content_hash": "version-one",
        "ts": 1234,
        "model_name": "old-backend/bge-small-zh-v1.5:q4_k_m",
    }
    assert storage.store_document_vectors(
        7,
        ["唯一分块"],
        [[0.1, 0.2]],
        metadata,
    )

    doc_key = metadata["doc_key"]
    assert storage.document_version_exists(doc_key, "version-one", 1)
    assert storage.document_version_exists(
        doc_key, "version-one", 1, "old-backend/bge-small-zh-v1.5:q4_k_m"
    )
    assert not storage.document_version_exists(
        doc_key, "version-one", 1, "BAAI/bge-small-zh-v1.5"
    )

    # 模型版本不一致时必须重写，而不是沿用旧空间向量。
    switched = {**metadata, "model_name": "BAAI/bge-small-zh-v1.5"}
    assert storage.store_document_vectors(
        7,
        ["唯一分块"],
        [[0.9, 0.8]],
        switched,
    )
    with sqlite3.connect(db_path) as conn:
        recorded = conn.execute(
            "SELECT model_name FROM vector_index"
        ).fetchall()
    assert recorded == [("BAAI/bge-small-zh-v1.5",)]


def test_artifact_document_vectors_use_durable_owner_and_retryable_deletion_queue(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = str(tmp_path / "durable-vectors.db")
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO bake_documents(id,title,full_content,updated_at) VALUES(80,'持久文档','第一块第二块',1234)")
    storage = VectorStorage(db_path=db_path)
    qdrant = _FakeQdrant()
    monkeypatch.setattr(storage, "_get_qdrant_client", lambda: qdrant)
    metadata = {
        "doc_key": "document_url:https://docs.example/durable",
        "content_hash": "version-one",
        "url": "https://docs.example/durable",
        "title": "持久文档",
        "updated_at": 1234,
    }
    current_point_id = storage._artifact_document_point_id(80, "version-one", 0)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO vector_deletion_queue (
                qdrant_point_id, source_type, reason, enqueued_at
            )
            VALUES (?, 'document', 'stale_rebuild_delete', 1)
            """,
            (current_point_id,),
        )

    assert storage.store_artifact_document_vectors(
        80,
        ["第一块", "第二块"],
        [[0.1, 0.2], [0.2, 0.3]],
        metadata,
    )
    assert storage.store_artifact_document_vectors(
        80,
        ["第一块", "第二块"],
        [[0.1, 0.2], [0.2, 0.3]],
        metadata,
    )
    assert len(qdrant.upserts) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM vector_deletion_queue"
        ).fetchone()[0] == 0

    changed = {**metadata, "content_hash": "version-two", "updated_at": 2345}
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE bake_documents SET updated_at=2345,full_content='新版本' WHERE id=80")
    assert storage.store_artifact_document_vectors(
        80,
        ["新版本"],
        [[0.3, 0.4]],
        changed,
    )
    with sqlite3.connect(db_path) as conn:
        ledger_rows = conn.execute(
            """
            SELECT document_id, content_hash, chunk_text
            FROM artifact_vector_index
            """
        ).fetchall()
        queued_count = conn.execute(
            "SELECT COUNT(*) FROM vector_deletion_queue"
        ).fetchone()[0]
    assert ledger_rows == [(80, "version-two", "新版本")]
    assert queued_count == 2

    drain_result = storage.drain_deletion_queue()
    assert drain_result["deleted_count"] == 2
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM vector_deletion_queue"
        ).fetchone()[0] == 0


def test_artifact_document_vectors_rebuild_after_embedding_backend_switch(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = str(tmp_path / "artifact-model-change.db")
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO bake_documents(id,title,full_content,updated_at) VALUES(81,'切换后端文档','旧空间分块',1234)")
    storage = VectorStorage(db_path=db_path)
    qdrant = _FakeQdrant()
    monkeypatch.setattr(storage, "_get_qdrant_client", lambda: qdrant)
    metadata = {
        "doc_key": "document_url:https://docs.example/switch",
        "content_hash": "version-one",
        "url": "https://docs.example/switch",
        "title": "切换后端文档",
        "updated_at": 1234,
        "model_name": "qllama/bge-small-zh-v1.5:q4_k_m",
    }

    assert storage.store_artifact_document_vectors(
        81,
        ["旧空间分块"],
        [[0.1, 0.2]],
        metadata,
    )
    assert storage.artifact_document_version_exists(
        81, metadata["doc_key"], "version-one", 1, "qllama/bge-small-zh-v1.5:q4_k_m"
    )
    assert not storage.artifact_document_version_exists(
        81, metadata["doc_key"], "version-one", 1, "BAAI/bge-small-zh-v1.5"
    )

    # 内容未变但嵌入后端已切换：同参数重复写入必须触发重建。
    switched = {**metadata, "model_name": "BAAI/bge-small-zh-v1.5"}
    assert storage.store_artifact_document_vectors(
        81,
        ["旧空间分块"],
        [[0.5, 0.6]],
        switched,
    )
    assert len(qdrant.upserts) == 2
    with sqlite3.connect(db_path) as conn:
        recorded = conn.execute(
            "SELECT model_name FROM artifact_vector_index WHERE document_id = 81"
        ).fetchall()
    assert recorded == [("BAAI/bge-small-zh-v1.5",)]


def test_bake_document_snapshot_does_not_need_a_capture() -> None:
    snapshot = build_bake_document_snapshot(
        {
            "id": 80,
            "title": "SMACT 指标说明",
            "full_content": "SMACT 用于衡量空分利用率。" * 40,
            "sections_json": "[]",
            "source_url": "https://docs.example.com/d/home/ABC?x=1",
        }
    )

    assert snapshot is not None
    assert snapshot.document_id == 80
    assert snapshot.doc_key == (
        "document_url:https://docs.example.com/d/home/ABC?x=1"
    )
    assert any("SMACT" in chunk for chunk in snapshot.chunks)


def test_vector_consistency_audit_is_read_only_until_orphans_are_enqueued(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = str(tmp_path / "vector-audit.db")
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO artifact_vector_index (
                document_id, qdrant_point_id, doc_key, content_hash,
                chunk_index, chunk_text, model_name, indexed_at
            )
            VALUES (80, 'expected-point', 'document:80', 'v1', 0, 'text', 'test', 1)
            """
        )
    storage = VectorStorage(db_path=db_path)
    qdrant = _FakeQdrant()
    qdrant.point_ids.update({"expected-point", "orphan-point"})
    monkeypatch.setattr(storage, "_get_qdrant_client", lambda: qdrant)

    dry_run = storage.audit_qdrant_consistency()
    assert dry_run["missing_count"] == 0
    assert dry_run["orphan_count"] == 1
    assert dry_run["orphans_enqueued"] == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM vector_deletion_queue"
        ).fetchone()[0] == 0

    reconciled = storage.audit_qdrant_consistency(enqueue_orphans=True)
    assert reconciled["orphans_enqueued"] == 1
    with sqlite3.connect(db_path) as conn:
        queued = conn.execute(
            "SELECT qdrant_point_id, reason FROM vector_deletion_queue"
        ).fetchall()
    assert queued == [("orphan-point", "qdrant_orphan_reconciliation")]

    qdrant.point_ids.remove("expected-point")
    repair = storage.audit_qdrant_consistency(mark_missing_artifacts=True)
    assert repair["missing_artifact_count"] == 1
    assert repair["missing_artifacts_marked_for_rebuild"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM artifact_vector_index"
        ).fetchone()[0] == 0


def test_background_vectorization_routes_document_chunks_to_document_domain(
    tmp_path,
    monkeypatch,
) -> None:
    class _Storage:
        def __init__(self) -> None:
            self.document_calls = []
            self.capture_calls = []

        def document_version_exists(self, *_args) -> bool:
            return False

        def store_document_vectors(self, capture_id, chunks, vectors, metadata):
            self.document_calls.append((capture_id, chunks, vectors, metadata))
            return True

        def store_vector(self, *args, **kwargs):
            self.capture_calls.append((args, kwargs))
            return True

    class _Model:
        model_name = "test-embedding"

        def encode(self, texts):
            return [
                EmbeddingVector(text=text, vector=[float(index), 0.5])
                for index, text in enumerate(texts)
            ]

    storage = _Storage()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("embedding.vector_storage.get_vector_storage", lambda: storage)
    monkeypatch.setattr("model_registry_global.get_shared_embedding", lambda: _Model())
    processor = BackgroundProcessor(db_path=str(tmp_path / "missing.db"))
    capture = {
        "id": 88,
        "ts": 1234,
        "app_name": "Google Chrome",
        "window_title": "潮汐调度说明",
        "url": "https://docs.example.com/k/home/sample-document",
        "ax_text": "背景信息。" * 100 + "潮汐特性用于调节后台任务。" * 60,
        "ocr_text": "",
    }

    asyncio.run(processor._process_vectorization_batch([capture]))

    assert storage.capture_calls == []
    assert len(storage.document_calls) == 1
    capture_id, chunks, vectors, metadata = storage.document_calls[0]
    assert capture_id == 88
    assert len(chunks) == len(vectors)
    assert len(chunks) >= 2
    assert any("潮汐特性" in chunk for chunk in chunks)
    assert metadata["source_type"] == "document"
    assert metadata["doc_key"].startswith("document_url:")


def test_background_backfills_vectors_from_bake_document_without_capture(
    tmp_path,
    monkeypatch,
) -> None:
    class _Storage:
        def __init__(self) -> None:
            self.calls = []

        def artifact_document_version_exists(self, *_args) -> bool:
            return False

        def store_artifact_document_vectors(
            self,
            document_id,
            chunks,
            vectors,
            metadata,
        ):
            self.calls.append((document_id, chunks, vectors, metadata))
            return True

    class _Model:
        model_name = "test-embedding"

        def encode(self, texts):
            return [
                EmbeddingVector(text=text, vector=[float(index), 0.5])
                for index, text in enumerate(texts)
            ]

    db_path = str(tmp_path / "bake-document.db")
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bake_documents (
                id, title, doc_type, full_content, source_url, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                80,
                "SMACT 指标说明",
                "技术文档",
                "SMACT 用于衡量空分利用率。" * 40,
                "https://docs.example.com/d/home/ABC",
                1234,
            ),
        )

    storage = _Storage()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("embedding.vector_storage.get_vector_storage", lambda: storage)
    monkeypatch.setattr("model_registry_global.get_shared_embedding", lambda: _Model())
    processor = BackgroundProcessor(db_path=db_path)

    result = asyncio.run(processor.backfill_bake_document_vectors())

    assert result == {"candidate_count": 1, "processed_count": 1}
    assert len(storage.calls) == 1
    document_id, chunks, vectors, metadata = storage.calls[0]
    assert document_id == 80
    assert len(chunks) == len(vectors)
    assert metadata["doc_key"].startswith("document_url:")


def test_load_pending_bake_documents_includes_stale_embedding_model(
    tmp_path,
) -> None:
    db_path = str(tmp_path / "stale-model.db")
    _create_durable_vector_schema(db_path)
    document = {
        "id": 90,
        "title": "稳柱的更新文档",
        "doc_type": "汇报",
        "summary": None,
        "full_content": "稳柱产品的更新内容说明。" * 40,
        "sections_json": "[]",
        "source_url": "https://docs.example.com/d/home/STALE",
        "updated_at": 2000,
    }
    snapshot = build_bake_document_snapshot(document)
    assert snapshot is not None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bake_documents (
                id, title, doc_type, full_content, source_url, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                document["id"],
                document["title"],
                document["doc_type"],
                document["full_content"],
                document["source_url"],
                document["updated_at"],
            ),
        )
        # 内容与版本均未变化，仅嵌入模型是旧后端的。
        for index, chunk in enumerate(snapshot.chunks):
            conn.execute(
                """
                INSERT INTO artifact_vector_index (
                    document_id, qdrant_point_id, doc_key, content_hash,
                    chunk_index, chunk_text, model_name, indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["id"],
                    f"stale-point-{index}",
                    snapshot.doc_key,
                    snapshot.content_hash,
                    index,
                    chunk,
                    "qllama/bge-small-zh-v1.5:q4_k_m",
                    3000,
                ),
            )

    processor = BackgroundProcessor(db_path=db_path)
    assert processor._load_pending_bake_documents(10) == []
    assert processor._load_pending_bake_documents(
        10,
        "BAAI/bge-small-zh-v1.5",
    ) == [{**document, "source_snapshot_id": None}]


def test_verified_short_source_is_scheduled_and_indexable(tmp_path):
    db_path = str(tmp_path / 'source.db')
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER)')
        conn.execute("INSERT INTO bake_documents(id,title,full_content,updated_at) VALUES(1,'短文','周一开始试运行。',100)")
        conn.execute('INSERT INTO bake_document_source_heads VALUES(1,7)')
        conn.execute('CREATE TABLE bake_document_source_snapshots(id INTEGER,document_id INTEGER,content_text TEXT,identity_match INTEGER,completeness_status TEXT)')
        conn.execute("INSERT INTO bake_document_source_snapshots SELECT 7,id,full_content,1,'complete' FROM bake_documents WHERE id=1")
    processor = BackgroundProcessor(db_path=db_path)
    documents = processor._load_pending_bake_documents(4, source_versions_only=True)
    assert len(documents) == 1
    assert documents[0]['source_snapshot_id'] == 7
    assert build_bake_document_snapshot(documents[0]).body == '周一开始试运行。'


def test_stale_head_cannot_schedule_or_publish_vectors_with_current_body_metadata(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'stale-source.db')
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        from pathlib import Path
        conn.executescript((Path(__file__).parents[2] / 'core-engine/src/storage/migrations/121_document_source_mismatch_events.sql').read_text())
        conn.execute('CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER)')
        conn.execute('CREATE TABLE bake_document_source_snapshots(id INTEGER,document_id INTEGER,content_text TEXT,identity_match INTEGER,completeness_status TEXT)')
        conn.execute("INSERT INTO bake_documents(id,title,full_content,updated_at) VALUES(1,'正文',?,100)", ('实际正文。' * 200,))
        conn.execute('INSERT INTO bake_document_source_heads VALUES(1,7)')
        conn.execute("INSERT INTO bake_document_source_snapshots VALUES(7,1,'失配的旧来源正文',1,'complete')")
    processor = BackgroundProcessor(db_path=db_path)
    assert processor._load_pending_bake_documents(4) == []
    assert processor._load_pending_bake_documents(4, source_versions_only=True) == []
    storage = VectorStorage(db_path=db_path)
    qdrant = _FakeQdrant()
    monkeypatch.setattr(storage, '_get_qdrant_client', lambda: qdrant)
    assert not storage.store_artifact_document_vectors(1, ['实际正文。'], [[.1, .2]], {
        'doc_key': 'document:1', 'content_hash': 'new', 'updated_at': 100, 'source_snapshot_id': 7,
    })
    assert qdrant.upserts == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT component,reason,SUM(occurrences) FROM document_source_mismatch_events GROUP BY component,reason ORDER BY component').fetchall() == [
            ('vector_schedule', 'head_invalid', 2), ('vector_write', 'head_invalid', 1)]


def test_source_change_during_embedding_cannot_publish_stale_index(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'race.db')
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute('CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER)')
        conn.execute("INSERT INTO bake_documents(id,title,full_content,updated_at) VALUES(1,'来源','旧文',100)")
        conn.execute('INSERT INTO bake_document_source_heads VALUES(1,7)')
        conn.execute('CREATE TABLE bake_document_source_snapshots(id INTEGER,document_id INTEGER,content_text TEXT,identity_match INTEGER,completeness_status TEXT)')
        conn.execute("INSERT INTO bake_document_source_snapshots SELECT 7,id,full_content,1,'complete' FROM bake_documents WHERE id=1")
    storage = VectorStorage(db_path=db_path)
    qdrant = _FakeQdrant()
    def racing_upsert(**kwargs):
        qdrant.upserts.append(kwargs)
        with sqlite3.connect(db_path) as conn:
            conn.execute('UPDATE bake_document_source_heads SET snapshot_id=8')
            conn.execute("UPDATE bake_documents SET updated_at=200,full_content='新文'")
    monkeypatch.setattr(qdrant, 'upsert', racing_upsert)
    monkeypatch.setattr(storage, '_get_qdrant_client', lambda: qdrant)
    metadata = {'doc_key':'document:1','content_hash':'old','updated_at':100,'source_snapshot_id':7}
    assert not storage.store_artifact_document_vectors(1,['旧文'],[[.1,.2]],metadata)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM artifact_vector_index').fetchone()[0] == 0
        assert conn.execute('SELECT reason FROM vector_deletion_queue').fetchone()[0] == 'source_changed_during_embedding'
    assert not storage.store_artifact_document_vectors(1,['旧文'],[[.1,.2]],metadata)
    assert len(qdrant.upserts) == 1


def test_unversioned_document_edit_during_embedding_rejects_stale_index(tmp_path, monkeypatch):
    import hashlib
    db_path = str(tmp_path / "unversioned-race.db")
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO bake_documents(id,title,full_content,updated_at) VALUES(1,'手动文档','旧文',100)")
    storage = VectorStorage(db_path=db_path)
    qdrant = _FakeQdrant()
    def racing_upsert(**kwargs):
        qdrant.upserts.append(kwargs)
        with sqlite3.connect(db_path) as conn:
            # Deliberately retain the timestamp to test body identity within one millisecond.
            conn.execute("UPDATE bake_documents SET full_content='用户已修改'")
    monkeypatch.setattr(qdrant, 'upsert', racing_upsert)
    monkeypatch.setattr(storage, '_get_qdrant_client', lambda: qdrant)
    metadata = {'doc_key':'document:1','content_hash':'old','updated_at':100,
                'source_body_hash':hashlib.sha256('旧文'.encode()).hexdigest()}
    assert not storage.store_artifact_document_vectors(1,['旧文'],[[.1,.2]],metadata)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM artifact_vector_index').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM vector_deletion_queue').fetchone()[0] == 1
    assert not storage.store_artifact_document_vectors(1,['旧文'],[[.1,.2]],metadata)
    assert len(qdrant.upserts) == 1


def test_document_shell_shared_cases():
    from pathlib import Path
    from embedding.document_quality import is_document_shell
    cases = json.loads((Path(__file__).resolve().parents[2] / 'shared/document-quality/cases.json').read_text())
    for case in cases:
        assert is_document_shell(case['text']) == case['shell'], case['name']
        if case['shell']:
            assert build_document_snapshot({'id': 1, 'url': 'https://docs.example.com/document/1', 'ax_text': case['text']}) is None
            assert build_bake_document_snapshot({'id': 1, 'full_content': case['text'], 'title': '测试文档'}) is None
            assert build_bake_document_snapshot({
                'id': 1, 'full_content': case['text'], 'title': '测试文档',
                'sections_json': json.dumps([{'content': 'This is a complete business procedure with detailed implementation instructions. ' * 1000}]),
            }) is None


class _PagedFakeQdrant:
    """A Qdrant stub whose ``scroll`` truly paginates (so it can be truncated by
    ``max_points``) while ``retrieve`` reports residency independently."""

    def __init__(self, point_ids, page_size: int) -> None:
        self._ids = sorted(str(point_id) for point_id in point_ids)
        self._page = max(1, int(page_size))
        self.retrieve_calls = 0

    @staticmethod
    def _point(point_id):
        class _Point:
            def __init__(self, value):
                self.id = value

        return _Point(point_id)

    def scroll(self, **kwargs):
        limit = int(kwargs.get("limit") or 1)
        offset = kwargs.get("offset")
        start = int(offset) if offset is not None else 0
        size = min(limit, self._page)
        page = self._ids[start : start + size]
        nxt = start + size
        next_offset = str(nxt) if nxt < len(self._ids) else None
        return [self._point(point_id) for point_id in page], next_offset

    def retrieve(self, **kwargs):
        self.retrieve_calls += 1
        present = set(self._ids)
        return [
            self._point(point_id)
            for point_id in kwargs.get("ids", [])
            if str(point_id) in present
        ]

    def upsert(self, **_kwargs):
        return None

    def delete(self, **_kwargs):
        return None


def test_backfill_bake_document_rebuilds_drifted_document_missing_from_qdrant(
    tmp_path,
    monkeypatch,
) -> None:
    """点2：账本看似已完成（非 pending）但 Qdrant 点已丢失的漂移文档，必须被
    驻留核对重新投入重建，而不是被 5 分钟一次的补齐通道永远 SKIP。
    """

    class _Storage:
        def __init__(self) -> None:
            self.calls = []

        def artifact_document_version_exists(self, *_args) -> bool:
            # 模拟漂移：账本还在，但 Qdrant 实点已丢。
            return False

        def store_artifact_document_vectors(
            self, document_id, chunks, vectors, metadata
        ):
            self.calls.append((document_id, chunks, vectors, metadata))
            return True

    class _Model:
        model_name = "test-embedding"

        def encode(self, texts):
            return [
                EmbeddingVector(text=text, vector=[float(index), 0.5])
                for index, text in enumerate(texts)
            ]

    db_path = str(tmp_path / "drift-residency.db")
    _create_durable_vector_schema(db_path)
    document = {
        "id": 80,
        "title": "GPU 资源池分布",
        "doc_type": "技术文档",
        "summary": None,
        "full_content": "万擎 GPU 资源池调度与分配说明。" * 40,
        "sections_json": "[]",
        "source_url": "https://docs.example.com/k/home/DRIFT",
        "updated_at": 1234,
    }
    snapshot = build_bake_document_snapshot(document)
    assert snapshot is not None
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bake_documents (
                id, title, doc_type, full_content, source_url, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                document["id"],
                document["title"],
                document["doc_type"],
                document["full_content"],
                document["source_url"],
                document["updated_at"],
            ),
        )
        # 账本看似已完成：indexed_at == updated_at、模型与当前一致 → HAVING 判为跳过。
        for index, chunk in enumerate(snapshot.chunks):
            conn.execute(
                """
                INSERT INTO artifact_vector_index (
                    document_id, qdrant_point_id, doc_key, content_hash,
                    chunk_index, chunk_text, model_name, indexed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document["id"],
                    f"drift-point-{index}",
                    snapshot.doc_key,
                    snapshot.content_hash,
                    index,
                    chunk,
                    "test-embedding",
                    1234,
                ),
            )

    storage = _Storage()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("embedding.vector_storage.get_vector_storage", lambda: storage)
    monkeypatch.setattr("model_registry_global.get_shared_embedding", lambda: _Model())
    processor = BackgroundProcessor(db_path=db_path)
    monkeypatch.setattr(
        processor, "_current_embedding_model_name", lambda: "test-embedding"
    )

    # 关键前提：只看账本的 pending 通道检不出这条漂移文档（返回空）。
    assert processor._load_pending_bake_documents(10, "test-embedding") == []

    result = asyncio.run(processor.backfill_bake_document_vectors())

    # 只有驻留核对把它捞回来，才能重建；否则 candidate_count 会是 0。
    assert result == {"candidate_count": 1, "processed_count": 1}
    assert [call[0] for call in storage.calls] == [80]


def test_consistency_audit_detects_missing_artifact_beyond_scroll_cap(
    tmp_path,
    monkeypatch,
) -> None:
    """点3：即使集合大于 max_points、孤儿 scroll 被截断（scan_truncated），
    “账本在、点不在”的漂移仍必须被检出来（旧实现会因截断把 missing 清零）。
    """
    db_path = str(tmp_path / "audit-cap.db")
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO artifact_vector_index (
                document_id, qdrant_point_id, doc_key, content_hash,
                chunk_index, chunk_text, model_name, indexed_at
            )
            VALUES (80, 'missing-point', 'document:80', 'v1', 0, 'text', 'test', 1)
            """
        )
    storage = VectorStorage(db_path=db_path)
    # 集合中只有大量孤儿点，目标 'missing-point' 实际不存在。
    stored = {f"orphan-{index}" for index in range(5)}
    qdrant = _PagedFakeQdrant(stored, page_size=2)
    monkeypatch.setattr(storage, "_get_qdrant_client", lambda: qdrant)

    result = storage.audit_qdrant_consistency(
        max_points=3,
        mark_missing_artifacts=True,
    )

    assert result["available"] is True
    # 孤儿扫描确实被 max_points 截断，旧逻辑会因此把 missing 强行清零。
    assert result["scan_truncated"] is True
    assert result["missing_count"] == 1
    assert result["missing_artifact_count"] == 1
    assert result["missing_artifacts_marked_for_rebuild"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM artifact_vector_index"
        ).fetchone()[0] == 0


def test_document_deletion_failure_keeps_retry_without_private_diagnostics(tmp_path, monkeypatch, caplog):
    import logging
    secret = 'PRIVATE_BODY https://docs.example.com/private?token=SECRET'
    db_path = str(tmp_path / 'private-delete.db')
    _create_durable_vector_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO vector_deletion_queue(qdrant_point_id,source_type,reason,enqueued_at) VALUES('pending-point','document','document_body_restored',1)")
    storage = VectorStorage(db_path=db_path)
    class BrokenClient:
        def delete(self, **kwargs):
            raise RuntimeError(secret)
    monkeypatch.setattr(storage, '_get_qdrant_client', lambda: BrokenClient())
    with caplog.at_level(logging.DEBUG):
        result = storage.drain_deletion_queue()
    assert result == {'selected_count': 1, 'deleted_count': 0, 'error': 'VECTOR_DELETE_FAILED'}
    with sqlite3.connect(db_path) as conn:
        row = conn.execute('SELECT qdrant_point_id,attempt_count,last_error,next_attempt_at FROM vector_deletion_queue').fetchone()
    assert row[:3] == ('pending-point', 1, 'VECTOR_DELETE_FAILED')
    assert row[3] > 0
    for fragment in ['PRIVATE_BODY', 'docs.example.com', 'SECRET']:
        assert fragment not in caplog.text + json.dumps(result) + str(row)
