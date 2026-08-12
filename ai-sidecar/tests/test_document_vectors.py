from __future__ import annotations

import asyncio
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


def test_document_snapshot_uses_canonical_url_and_full_ax_text() -> None:
    capture = {
        "id": 9,
        "url": "https://docs.example.com/k/home/sample-document?from=recent#section",
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


def test_artifact_document_vectors_use_durable_owner_and_retryable_deletion_queue(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = str(tmp_path / "durable-vectors.db")
    _create_durable_vector_schema(db_path)
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
        "document_url:https://docs.example.com/d/home/ABC"
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
