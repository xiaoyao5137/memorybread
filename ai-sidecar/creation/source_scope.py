"""Exact, bounded excerpts of a versioned source; no normalized-text offsets."""
import hashlib
from typing import Optional


def source_excerpt(text: str, limit: int, snapshot_id: int,
                   coverage: str, source_hash: Optional[str] = None) -> dict:
    body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if source_hash is not None and source_hash != body_hash:
        raise ValueError("Source body hash does not match reference content")
    excerpt = text[:max(0, limit)]
    raw = excerpt.encode("utf-8")
    return {"content": excerpt, "source_scope": {
        "schema_version": "document-excerpt.v1", "snapshot_id": snapshot_id,
        "source_body_hash": body_hash, "offset_unit": "utf8_bytes",
        "start_byte": 0, "end_byte": len(raw),
        "excerpt_hash": hashlib.sha256(raw).hexdigest(),
        "source_coverage": coverage, "excerpt_truncated": len(excerpt) < len(text),
        "allows_full_document_claims": coverage == "complete" and len(excerpt) == len(text),
    }}


def shorten_source_excerpt(text: str, scope: dict, limit: int) -> dict:
    """Validate the prior view before narrowing it; never widen source claims."""
    raw = text.encode("utf-8")
    start = scope.get("start_byte")
    end = scope.get("end_byte")
    if (scope.get("schema_version") != "document-excerpt.v1"
            or scope.get("offset_unit") != "utf8_bytes"
            or type(start) is not int or type(end) is not int
            or start < 0 or end - start != len(raw)
            or scope.get("excerpt_hash") != hashlib.sha256(raw).hexdigest()):
        raise ValueError("Source excerpt scope does not match reference content")
    excerpt = text[:max(0, limit)]
    updated = dict(scope)
    updated.update(end_byte=start + len(excerpt.encode("utf-8")),
                   excerpt_hash=hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                   excerpt_truncated=bool(scope.get("excerpt_truncated")) or len(excerpt) < len(text))
    updated["allows_full_document_claims"] = (
        scope.get("allows_full_document_claims") is True
        and not updated["excerpt_truncated"])
    return {"content": excerpt, "source_scope": updated}
