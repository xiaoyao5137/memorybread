"""
向量存储管理器

负责将向量写入 Qdrant 和 SQLite vector_index 表
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Union
from pathlib import Path

logger = logging.getLogger(__name__)


class VectorStorage:
    """向量存储管理器"""
    
    def __init__(
        self,
        db_path: Optional[str] = None,
        qdrant_path: Optional[str] = None,
        qdrant_host: str = "localhost",
        qdrant_port: int = 6333,
    ):
        """
        初始化向量存储

        Args:
            db_path: SQLite 数据库路径
            qdrant_path: Qdrant 本地存储路径（优先使用）
            qdrant_host: Qdrant 服务地址
            qdrant_port: Qdrant 服务端口
        """
        self.db_path = db_path or str(Path.home() / ".memory-bread" / "memory-bread.db")
        self.qdrant_path = qdrant_path
        self.qdrant_host = qdrant_host
        self.qdrant_port = qdrant_port
        self._qdrant_client = None
        self._collection_name = "memory_bread_captures"

        logger.info(f"VectorStorage 初始化: db={self.db_path}, qdrant_path={qdrant_path}")
    
    def _get_qdrant_client(self):
        """懒加载 Qdrant 客户端"""
        if self._qdrant_client is None:
            try:
                from qdrant_client import QdrantClient
                from qdrant_client.models import Distance, VectorParams

                # 使用统一的 Qdrant 本地路径
                qdrant_path = Path.home() / ".qdrant"
                qdrant_path.mkdir(parents=True, exist_ok=True)

                logger.info(f"使用 Qdrant 本地模式: {qdrant_path}")
                self._qdrant_client = QdrantClient(path=str(qdrant_path))

                # 主进程/检索器已占用本地目录时，后台向量化降级为仅写 SQLite，不阻断时间线提炼
                # 这里保留客户端初始化逻辑；失败时由 store_vector() 做降级处理

                # 确保集合存在
                collections = self._qdrant_client.get_collections().collections
                collection_names = [c.name for c in collections]
                
                if self._collection_name not in collection_names:
                    logger.info(f"创建 Qdrant 集合: {self._collection_name}")
                    self._qdrant_client.create_collection(
                        collection_name=self._collection_name,
                        vectors_config=VectorParams(
                            size=512,  # bge-small-zh-v1.5 维度
                            distance=Distance.COSINE,
                        ),
                    )
                
                logger.info("Qdrant 客户端已连接")
            except Exception as e:
                logger.error(f"连接 Qdrant 失败: {e}")
                self._qdrant_client = None
        
        return self._qdrant_client

    def is_qdrant_available(self) -> bool:
        """Return whether this process can currently write the vector store."""
        return self._get_qdrant_client() is not None

    @staticmethod
    def _document_point_id(doc_key: str, content_hash: str, chunk_index: int) -> str:
        """Stable Qdrant id: unchanged document chunks are naturally idempotent."""
        value = f"memory-bread:{doc_key}:{content_hash}:{chunk_index}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, value))

    @staticmethod
    def _artifact_document_point_id(
        document_id: int,
        content_hash: str,
        chunk_index: int,
    ) -> str:
        """Stable point id whose durable owner is ``bake_documents.id``."""
        value = f"memory-bread:bake-document:{document_id}:{content_hash}:{chunk_index}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, value))

    def document_version_exists(
        self,
        doc_key: str,
        content_hash: str,
        chunk_count: int,
        expected_model_name: Optional[str] = None,
    ) -> bool:
        expected = {
            self._document_point_id(doc_key, content_hash, index)
            for index in range(chunk_count)
        }
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT qdrant_point_id, model_name
                    FROM vector_index
                    WHERE doc_key = ? AND source_type = 'document'
                    """,
                    (doc_key,),
                ).fetchall()
            if not (bool(expected) and {str(row[0]) for row in rows} == expected):
                return False
            # 嵌入后端切换后（如 Ollama 量化 -> sentence-transformers）同名模型
            # 的向量空间不兼容，旧模型索引的向量视为过期版本，需要重建。
            if expected_model_name and any(
                str(row[1] or "") != expected_model_name for row in rows
            ):
                return False
            return True
        except sqlite3.Error as exc:
            logger.warning("检查文档向量版本失败: doc_key=%s error=%s", doc_key, exc)
            return False

    def artifact_document_version_exists(
        self,
        document_id: int,
        doc_key: str,
        content_hash: str,
        chunk_count: int,
        expected_model_name: Optional[str] = None,
    ) -> bool:
        """Check both the durable SQLite ledger and Qdrant when available."""
        expected = {
            self._artifact_document_point_id(document_id, content_hash, index)
            for index in range(chunk_count)
        }
        if not expected:
            return False
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT qdrant_point_id, model_name
                    FROM artifact_vector_index
                    WHERE document_id = ? AND content_hash = ?
                    """,
                    (document_id, content_hash),
                ).fetchall()
            recorded = {str(row[0]) for row in rows}
            if recorded != expected:
                return False
            # 嵌入后端切换后（如 Ollama 量化 -> sentence-transformers）同名模型
            # 的向量空间不兼容，旧模型索引的向量视为过期版本，需要重建。
            if expected_model_name and any(
                str(row[1] or "") != expected_model_name for row in rows
            ):
                return False

            qdrant_client = self._get_qdrant_client()
            if qdrant_client is None or not hasattr(qdrant_client, "retrieve"):
                return True
            points = qdrant_client.retrieve(
                collection_name=self._collection_name,
                ids=list(expected),
                with_payload=False,
                with_vectors=False,
            )
            return {str(point.id) for point in points} == expected
        except sqlite3.Error as exc:
            logger.warning(
                "检查持久文档向量版本失败: document_id=%s error=%s",
                document_id,
                exc,
            )
            return False
        except Exception as exc:
            logger.warning(
                "核验 Qdrant 持久文档向量失败，标记为待修复: document_id=%s error=%s",
                document_id,
                exc,
            )
            return False

    def store_artifact_document_vectors(
        self,
        document_id: int,
        chunks: List[str],
        vectors: List[List[float]],
        metadata: Dict[str, Any],
    ) -> bool:
        """Persist vectors owned by a bake document rather than a raw capture."""
        if document_id <= 0 or not chunks or len(chunks) != len(vectors):
            logger.error(
                "持久文档向量参数无效: document_id=%s chunks=%s vectors=%s",
                document_id,
                len(chunks),
                len(vectors),
            )
            return False

        metadata = dict(metadata or {})
        doc_key = str(metadata.get("doc_key") or "").strip()
        content_hash = str(metadata.get("content_hash") or "").strip()
        if not doc_key or not content_hash:
            logger.error("持久文档向量缺少稳定键: document_id=%s", document_id)
            return False
        if self.artifact_document_version_exists(
            document_id,
            doc_key,
            content_hash,
            len(chunks),
            metadata.get("model_name"),
        ):
            return True

        point_ids = [
            self._artifact_document_point_id(document_id, content_hash, index)
            for index in range(len(chunks))
        ]
        indexed_at = int(metadata.get("updated_at") or time.time() * 1000)
        payloads = [
            {
                "doc_key": doc_key,
                "source_type": "document",
                "capture_id": 0,
                "document_id": document_id,
                "knowledge_id": None,
                "time": indexed_at,
                "ts": indexed_at,
                "observed_at": indexed_at,
                "history_view": False,
                "content_origin": "bake_document",
                "activity_type": "document",
                "is_self_generated": False,
                "evidence_strength": "high",
                "category": metadata.get("doc_type") or "文档",
                "url": metadata.get("url"),
                "source_url": metadata.get("url"),
                "title": metadata.get("title"),
                "content_hash": content_hash,
                "chunk_index": index,
                "chunk_count": len(chunks),
                "text": text,
            }
            for index, text in enumerate(chunks)
        ]

        try:
            qdrant_client = self._get_qdrant_client()
            if qdrant_client is None:
                logger.warning(
                    "Qdrant 不可用，持久文档不登记完成: document_id=%s",
                    document_id,
                )
                return False

            from qdrant_client.models import PointStruct

            qdrant_client.upsert(
                collection_name=self._collection_name,
                points=[
                    PointStruct(id=point_id, vector=vector, payload=payload)
                    for point_id, vector, payload in zip(point_ids, vectors, payloads)
                ],
            )

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM artifact_vector_index WHERE document_id = ?",
                    (document_id,),
                )
                conn.executemany(
                    """
                    INSERT INTO artifact_vector_index (
                        document_id, qdrant_point_id, doc_key, content_hash,
                        chunk_index, chunk_text, model_name, indexed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            document_id,
                            point_id,
                            doc_key,
                            content_hash,
                            index,
                            text,
                            metadata.get("model_name", "bge-small-zh-v1.5"),
                            indexed_at,
                        )
                        for index, (point_id, text) in enumerate(zip(point_ids, chunks))
                    ],
                )
                placeholders = ", ".join("?" for _ in point_ids)
                conn.execute(
                    f"DELETE FROM vector_deletion_queue "
                    f"WHERE qdrant_point_id IN ({placeholders})",
                    point_ids,
                )

            logger.info(
                "✅ 持久文档分块向量完成: document_id=%s doc_key=%s chunks=%s",
                document_id,
                doc_key,
                len(chunks),
            )
            return True
        except Exception as exc:
            # point id 稳定；若 Qdrant 成功而 SQLite 失败，下次重试会幂等覆盖。
            logger.error(
                "❌ 持久文档向量存储失败: document_id=%s doc_key=%s error=%s",
                document_id,
                doc_key,
                exc,
                exc_info=True,
            )
            return False

    def drain_deletion_queue(self, limit: int = 256) -> dict[str, Union[int, str]]:
        """Drain the SQLite deletion outbox into Qdrant idempotently."""
        now_ms = int(time.time() * 1000)
        try:
            with sqlite3.connect(self.db_path) as conn:
                table_exists = conn.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'table' AND name = 'vector_deletion_queue'
                    )
                    """
                ).fetchone()
                if not table_exists or not table_exists[0]:
                    return {"selected_count": 0, "deleted_count": 0}
                rows = conn.execute(
                    """
                    SELECT qdrant_point_id, attempt_count
                    FROM vector_deletion_queue
                    WHERE next_attempt_at <= ?
                    ORDER BY enqueued_at
                    LIMIT ?
                    """,
                    (now_ms, max(1, int(limit))),
                ).fetchall()
            point_ids = [str(row[0]) for row in rows]
            if not point_ids:
                return {"selected_count": 0, "deleted_count": 0}

            qdrant_client = self._get_qdrant_client()
            if qdrant_client is None:
                return {
                    "selected_count": len(point_ids),
                    "deleted_count": 0,
                    "error": "qdrant_unavailable",
                }

            from qdrant_client.models import PointIdsList

            qdrant_client.delete(
                collection_name=self._collection_name,
                points_selector=PointIdsList(points=point_ids),
            )
            placeholders = ", ".join("?" for _ in point_ids)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    f"DELETE FROM vector_deletion_queue "
                    f"WHERE qdrant_point_id IN ({placeholders})",
                    point_ids,
                )
            return {
                "selected_count": len(point_ids),
                "deleted_count": len(point_ids),
            }
        except Exception as exc:
            retry_at = now_ms + 30_000
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.executemany(
                        """
                        UPDATE vector_deletion_queue
                        SET attempt_count = attempt_count + 1,
                            last_error = ?,
                            next_attempt_at = ?
                        WHERE qdrant_point_id = ?
                        """,
                        [
                            (str(exc)[:500], retry_at, str(row[0]))
                            for row in locals().get("rows", [])
                        ],
                    )
            except sqlite3.Error:
                pass
            logger.warning("Qdrant 删除队列消费失败: %s", exc)
            return {
                "selected_count": len(locals().get("point_ids", [])),
                "deleted_count": 0,
                "error": str(exc),
            }

    def audit_qdrant_consistency(
        self,
        *,
        enqueue_orphans: bool = False,
        mark_missing_artifacts: bool = False,
        max_points: int = 50_000,
    ) -> dict[str, Any]:
        """Compare the SQLite ledgers with Qdrant.

        The default is read-only.  ``mark_missing_artifacts=True`` clears only
        durable ledger rows whose Qdrant points disappeared, allowing the
        regular backfill to rebuild them.  ``enqueue_orphans=True`` records
        orphan deletions in the outbox; the background worker performs the
        actual Qdrant deletion later.
        """
        artifact_expected: set[str] = set()
        with sqlite3.connect(self.db_path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table'
                      AND name IN ('vector_index', 'artifact_vector_index')
                    """
                )
            }
            expected: set[str] = set()
            if "vector_index" in tables:
                expected.update(
                    str(row[0])
                    for row in conn.execute(
                        "SELECT qdrant_point_id FROM vector_index"
                    )
                )
            if "artifact_vector_index" in tables:
                artifact_expected.update(
                    str(row[0])
                    for row in conn.execute(
                        "SELECT qdrant_point_id FROM artifact_vector_index"
                    )
                )
                expected.update(artifact_expected)
        qdrant_client = self._get_qdrant_client()
        if qdrant_client is None:
            return {"available": False, "expected_count": len(expected)}

        actual: set[str] = set()
        offset = None
        while len(actual) < max_points:
            points, offset = qdrant_client.scroll(
                collection_name=self._collection_name,
                limit=min(512, max_points - len(actual)),
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            actual.update(str(point.id) for point in points)
            if offset is None or not points:
                break

        scan_truncated = len(actual) >= max_points and offset is not None
        # A partial scroll can prove an orphan exists, but cannot prove an
        # expected point is missing.
        missing = set() if scan_truncated else expected.difference(actual)
        orphans = actual.difference(expected)
        missing_artifacts = missing.intersection(artifact_expected)
        marked_missing_artifacts = 0
        if mark_missing_artifacts and missing_artifacts:
            placeholders = ", ".join("?" for _ in missing_artifacts)
            with sqlite3.connect(self.db_path) as conn:
                marked_missing_artifacts = conn.execute(
                    f"DELETE FROM artifact_vector_index "
                    f"WHERE qdrant_point_id IN ({placeholders})",
                    list(missing_artifacts),
                ).rowcount
        if enqueue_orphans and orphans:
            now_ms = int(time.time() * 1000)
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO vector_deletion_queue (
                        qdrant_point_id, source_type, reason, enqueued_at
                    )
                    VALUES (?, 'unknown', 'qdrant_orphan_reconciliation', ?)
                    """,
                    [(point_id, now_ms) for point_id in orphans],
                )
        return {
            "available": True,
            "expected_count": len(expected),
            "actual_count": len(actual),
            "missing_count": len(missing),
            "missing_artifact_count": len(missing_artifacts),
            "missing_artifacts_marked_for_rebuild": marked_missing_artifacts,
            "orphan_count": len(orphans),
            "scan_truncated": scan_truncated,
            "missing_sample": sorted(missing)[:20],
            "orphan_sample": sorted(orphans)[:20],
            "orphans_enqueued": len(orphans) if enqueue_orphans else 0,
        }

    def store_document_vectors(
        self,
        capture_id: int,
        chunks: List[str],
        vectors: List[List[float]],
        metadata: Dict[str, Any],
    ) -> bool:
        """Atomically replace one URL document's vector chunks.

        All chunks share the URL-level ``doc_key`` so retrieval can keep the
        best matching chunk without allowing one long document to occupy every
        context slot.
        """
        if not chunks or len(chunks) != len(vectors):
            logger.error(
                "文档向量数量不匹配: capture_id=%s chunks=%s vectors=%s",
                capture_id,
                len(chunks),
                len(vectors),
            )
            return False

        metadata = dict(metadata or {})
        doc_key = str(metadata.get("doc_key") or "").strip()
        content_hash = str(metadata.get("content_hash") or "").strip()
        if not doc_key or not content_hash:
            logger.error("文档向量缺少稳定键: capture_id=%s", capture_id)
            return False

        point_ids = [
            self._document_point_id(doc_key, content_hash, index)
            for index in range(len(chunks))
        ]
        if self.document_version_exists(
            doc_key,
            content_hash,
            len(chunks),
            metadata.get("model_name"),
        ):
            logger.debug("文档向量未变化，跳过重写: doc_key=%s", doc_key)
            return True

        time_value = metadata.get("ts") or metadata.get("timestamp")
        payloads = []
        for index, text in enumerate(chunks):
            payloads.append(
                {
                    "doc_key": doc_key,
                    "source_type": "document",
                    "capture_id": capture_id,
                    "knowledge_id": None,
                    "time": time_value,
                    "ts": time_value,
                    "start_time": None,
                    "end_time": None,
                    "observed_at": time_value,
                    "event_time_start": None,
                    "event_time_end": None,
                    "history_view": False,
                    "content_origin": "document_reference",
                    "activity_type": "reading",
                    "is_self_generated": False,
                    "evidence_strength": "medium",
                    "app_name": metadata.get("app_name"),
                    "win_title": metadata.get("win_title"),
                    "category": "文档",
                    "user_verified": False,
                    "url": metadata.get("url"),
                    "source_url": metadata.get("url"),
                    "title": metadata.get("title"),
                    "content_hash": content_hash,
                    "chunk_index": index,
                    "chunk_count": len(chunks),
                    "text": text,
                }
            )

        try:
            with sqlite3.connect(self.db_path) as conn:
                existing_ids = {
                    str(row[0])
                    for row in conn.execute(
                        """
                        SELECT qdrant_point_id
                        FROM vector_index
                        WHERE doc_key = ? AND source_type = 'document'
                        """,
                        (doc_key,),
                    ).fetchall()
                }

            qdrant_client = self._get_qdrant_client()
            if qdrant_client:
                from qdrant_client.models import PointStruct

                qdrant_client.upsert(
                    collection_name=self._collection_name,
                    points=[
                        PointStruct(id=point_id, vector=vector, payload=payload)
                        for point_id, vector, payload in zip(point_ids, vectors, payloads)
                    ],
                )
            else:
                logger.warning("Qdrant 不可用，文档分块仅写 SQLite vector_index")

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "DELETE FROM vector_index WHERE doc_key = ? AND source_type = 'document'",
                    (doc_key,),
                )
                conn.executemany(
                    """
                    INSERT INTO vector_index
                    (capture_id, qdrant_point_id, chunk_index, chunk_text, model_name, created_at,
                     doc_key, source_type, knowledge_id, time, start_time, end_time,
                     observed_at, event_time_start, event_time_end, history_view,
                     content_origin, activity_type, is_self_generated, evidence_strength,
                     app_name, win_title, category, user_verified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'document', NULL, ?, NULL, NULL,
                            ?, NULL, NULL, 0, 'document_reference', 'reading', 0, 'medium',
                            ?, ?, '文档', 0)
                    """,
                    [
                        (
                            capture_id,
                            point_id,
                            index,
                            text,
                            metadata.get("model_name", "bge-small-zh-v1.5"),
                            int(time_value or 0),
                            doc_key,
                            time_value,
                            time_value,
                            metadata.get("app_name"),
                            metadata.get("win_title"),
                        )
                        for index, (point_id, text) in enumerate(zip(point_ids, chunks))
                    ],
                )
                deletion_queue_exists = conn.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'table' AND name = 'vector_deletion_queue'
                    )
                    """
                ).fetchone()
                if deletion_queue_exists and deletion_queue_exists[0]:
                    placeholders = ", ".join("?" for _ in point_ids)
                    conn.execute(
                        f"DELETE FROM vector_deletion_queue "
                        f"WHERE qdrant_point_id IN ({placeholders})",
                        point_ids,
                    )

            stale_ids = existing_ids.difference(point_ids)
            if qdrant_client and stale_ids:
                try:
                    from qdrant_client.models import PointIdsList

                    qdrant_client.delete(
                        collection_name=self._collection_name,
                        points_selector=PointIdsList(points=list(stale_ids)),
                    )
                except Exception as exc:
                    logger.warning("清理旧文档向量失败，后续检索仍会按 URL 折叠: %s", exc)

            logger.info(
                "✅ 文档分块向量完成: capture_id=%s doc_key=%s chunks=%s",
                capture_id,
                doc_key,
                len(chunks),
            )
            return True
        except Exception as exc:
            logger.error(
                "❌ 文档分块向量存储失败: capture_id=%s doc_key=%s error=%s",
                capture_id,
                doc_key,
                exc,
                exc_info=True,
            )
            return False
    
    def store_vector(
        self,
        capture_id: int,
        text: str,
        vector: List[float],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        存储向量到 Qdrant 和 SQLite

        Args:
            capture_id: 采集记录 ID
            text: 原始文本
            vector: 向量数据
            metadata: 额外元数据（app_name, timestamp 等）

        Returns:
            是否成功
        """
        try:
            metadata = dict(metadata or {})
            point_id = str(uuid.uuid4())
            source_type = metadata.get("source_type") or "capture"
            knowledge_id = metadata.get("knowledge_id")
            doc_key = metadata.get("doc_key")
            if not doc_key:
                doc_key = f"knowledge:{knowledge_id}" if source_type == "knowledge" and knowledge_id is not None else f"capture:{capture_id}"

            if source_type == "knowledge":
                time_value = metadata.get("end_time") or metadata.get("start_time")
            else:
                time_value = metadata.get("ts") or metadata.get("timestamp")

            payload = {
                "doc_key": doc_key,
                "source_type": source_type,
                "capture_id": capture_id,
                "knowledge_id": knowledge_id,
                "time": time_value,
                "ts": metadata.get("ts") or metadata.get("timestamp"),
                "start_time": metadata.get("start_time"),
                "end_time": metadata.get("end_time"),
                "observed_at": metadata.get("observed_at"),
                "event_time_start": metadata.get("event_time_start"),
                "event_time_end": metadata.get("event_time_end"),
                "history_view": bool(metadata.get("history_view", False)),
                "content_origin": metadata.get("content_origin"),
                "activity_type": metadata.get("activity_type"),
                "is_self_generated": bool(metadata.get("is_self_generated", False)),
                "evidence_strength": metadata.get("evidence_strength"),
                "app_name": metadata.get("app_name"),
                "win_title": metadata.get("win_title"),
                "category": metadata.get("category"),
                "user_verified": bool(metadata.get("user_verified", False)),
                # 普通 activity 本身仍很短；文档长文本走 store_document_vectors，
                # 在模型上下文范围内完整保存每个 chunk，不再二次截断。
                "text": text,
            }

            qdrant_client = self._get_qdrant_client()
            if qdrant_client:
                from qdrant_client.models import PointStruct

                point = PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload,
                )

                qdrant_client.upsert(
                    collection_name=self._collection_name,
                    points=[point],
                )

                logger.debug(f"向量已写入 Qdrant: {point_id}")
            else:
                logger.warning("Qdrant 不可用，降级为仅写 SQLite vector_index")

            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                """
                INSERT INTO vector_index
                (capture_id, qdrant_point_id, chunk_index, chunk_text, model_name, created_at,
                 doc_key, source_type, knowledge_id, time, start_time, end_time,
                 observed_at, event_time_start, event_time_end, history_view,
                 content_origin, activity_type, is_self_generated, evidence_strength,
                 app_name, win_title, category, user_verified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    point_id,
                    int(metadata.get("chunk_index", 0)),
                    text,
                    metadata.get("model_name", "bge-small-zh-v1.5"),
                    int(time_value or 0),
                    doc_key,
                    source_type,
                    knowledge_id,
                    time_value,
                    metadata.get("start_time"),
                    metadata.get("end_time"),
                    metadata.get("observed_at"),
                    metadata.get("event_time_start"),
                    metadata.get("event_time_end"),
                    1 if metadata.get("history_view") else 0,
                    metadata.get("content_origin"),
                    metadata.get("activity_type"),
                    1 if metadata.get("is_self_generated") else 0,
                    metadata.get("evidence_strength"),
                    metadata.get("app_name"),
                    metadata.get("win_title"),
                    metadata.get("category"),
                    1 if metadata.get("user_verified") else 0,
                ),
            )

            conn.commit()
            conn.close()

            logger.info(
                "✅ 向量存储完成: capture_id=%s, doc_key=%s, source_type=%s, point_id=%s",
                capture_id,
                doc_key,
                source_type,
                point_id,
            )
            return True

        except Exception as e:
            logger.error(f"❌ 向量存储失败: {e}", exc_info=True)
            return False


# 全局单例
_vector_storage = None


def get_vector_storage() -> VectorStorage:
    """获取全局向量存储单例"""
    global _vector_storage
    if _vector_storage is None:
        _vector_storage = VectorStorage()
    return _vector_storage
