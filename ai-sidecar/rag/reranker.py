"""
RRF (Reciprocal Rank Fusion) 多路结果合并器

将多路检索结果（FTS5、向量等）合并为统一排序列表，
消除不同打分量纲（BM25 vs 余弦相似度）的影响。

RRF 公式：score(d) = Σ [ 1 / (k + rank(d, list_i)) ]
常数 k=60（Cormack et al., 2009 推荐默认值）
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

from .retriever import RetrievedChunk


WeightedResultList = tuple[list[RetrievedChunk], float]

# 烘焙产物来源：同 doc_key 冲突时其内容始终优先于原始采集兜底，
# 避免 capture FTS 的高 BM25 原始分抢占已烘焙文档的正文。
_ARTIFACT_SOURCE_TYPES = {"document", "bake_knowledge", "operation", "data", "knowledge"}


def _is_artifact_chunk(chunk: RetrievedChunk) -> bool:
    source_type = str((chunk.metadata or {}).get("source_type") or chunk.source or "")
    return source_type in _ARTIFACT_SOURCE_TYPES


def _fusion_doc_key(chunk: RetrievedChunk) -> str:
    metadata = chunk.metadata or {}
    source_type = str(metadata.get("source_type") or chunk.source or "")
    if source_type == "document":
        document_id = metadata.get("document_id") or metadata.get("artifact_id")
        if document_id is not None and str(document_id).strip():
            return f"document:{document_id}"
    return chunk.doc_key or metadata.get("doc_key") or f"capture:{chunk.capture_id}"


def reciprocal_rank_fusion(
    result_lists: Union[list[list[RetrievedChunk]], list[WeightedResultList]],
    top_k:        int = 10,
    k:            int = 60,
    min_score:    float = 0.0,
) -> list[RetrievedChunk]:
    """
    对多路检索结果执行 RRF 合并。

    Args:
        result_lists: 多路检索结果（每路已按相关性降序排列）。
                      可传 [(results, weight), ...] 为不同召回源设置权重。
        top_k:        最终返回的 Top-K 结果数量
        k:            RRF 常数（默认 60）
        min_score:    最小融合分数，低于该值的结果会被丢弃

    Returns:
        合并并重新排序的 RetrievedChunk 列表（source="merged"）
    """
    rrf_scores: dict[str, float] = {}
    best_chunk: dict[str, RetrievedChunk] = {}

    for entry in result_lists:
        results, weight = _normalize_result_list(entry)
        if weight <= 0:
            continue
        for rank, chunk in enumerate(results):
            doc_key = _fusion_doc_key(chunk)
            rrf_scores[doc_key] = rrf_scores.get(doc_key, 0.0) + weight / (k + rank + 1)
            current = best_chunk.get(doc_key)
            is_artifact = _is_artifact_chunk(chunk)
            if (
                current is None
                or (is_artifact and not _is_artifact_chunk(current))
                or (is_artifact == _is_artifact_chunk(current) and chunk.score > current.score)
            ):
                best_chunk[doc_key] = chunk

    sorted_doc_keys = [
        doc_key
        for doc_key in sorted(rrf_scores, key=lambda key: rrf_scores[key], reverse=True)
        if rrf_scores[doc_key] >= min_score
    ][:top_k]

    return [
        RetrievedChunk(
            capture_id=best_chunk[doc_key].capture_id,
            text=best_chunk[doc_key].text,
            score=rrf_scores[doc_key],
            source="merged",
            doc_key=doc_key,
            metadata={
                **best_chunk[doc_key].metadata,
                "doc_key": doc_key,
                "retrieval_score": best_chunk[doc_key].score,
                "rrf_score": rrf_scores[doc_key],
                "source_type": best_chunk[doc_key].metadata.get("source_type", best_chunk[doc_key].source),
            },
        )
        for doc_key in sorted_doc_keys
    ]


def _normalize_result_list(entry: Union[list[RetrievedChunk], WeightedResultList]) -> WeightedResultList:
    if (
        isinstance(entry, tuple)
        and len(entry) == 2
        and isinstance(entry[1], (int, float))
    ):
        return entry[0], float(entry[1])
    if isinstance(entry, Sequence):
        return list(entry), 1.0
    return [], 1.0
