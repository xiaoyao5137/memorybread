"""Document-aware text chunking for capture embeddings.

Raw captures are still embedded as one short activity summary.  A capture that
points at a document URL is different: its AX tree can contain the complete
article, so cutting it at 500 characters silently removes most of the useful
content.  This module turns that document snapshot into bounded, overlapping
chunks that fit the 512-token BGE context window.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from knowledge.fragment_grouper import _is_document_url
from .document_quality import is_document_shell


DOCUMENT_CHUNK_TARGET_TOKENS = 420
DOCUMENT_CHUNK_OVERLAP_TOKENS = 64
MAX_DOCUMENT_CHUNKS = 24
MIN_DOCUMENT_CHARS = 200

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+(?=[A-Z0-9])")
_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+|\d+(?:\.\d+)*[、.)）]\s*|[一二三四五六七八九十]+[、.)）]\s*)"
)


@dataclass(frozen=True)
class DocumentSnapshot:
    capture_id: int
    canonical_url: str
    doc_key: str
    title: str
    body: str
    content_hash: str
    chunks: list[str]


@dataclass(frozen=True)
class BakeDocumentSnapshot:
    document_id: int
    canonical_url: Optional[str]
    doc_key: str
    title: str
    body: str
    content_hash: str
    chunks: list[str]


def _canonicalize_url(url: Optional[str]) -> Optional[str]:
    value = str(url or "").strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError):
        return None
    scheme = parsed.scheme.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port is not None and port != (443 if scheme == "https" else 80):
        host += ":" + str(port)
    path = parsed.path or "/"
    query, fragment = parsed.query, parsed.fragment
    if path.startswith(("/d/home/", "/s/home/", "/k/home/")):
        pairs = []
        for pair in query.split("&"):
            key, _, val = pair.partition("=")
            if key == "section" or (key == "ro" and val in ("true", "false")):
                continue
            pairs.append(pair)
        query, fragment = "&".join(pairs), ""
    return urlunsplit((scheme, host, path, query, fragment))


def canonicalize_document_url(url: Optional[str]) -> Optional[str]:
    """Preserve identity-bearing URL components; ignore declared editor view controls."""
    value = str(url or "").strip()
    if not value or not _is_document_url(value):
        return None
    return _canonicalize_url(value)


def document_doc_key(canonical_url: str) -> str:
    return f"document_url:{canonical_url}"


def estimate_tokens(text: str) -> int:
    """Cheap tokenizer-independent estimate for Chinese/English document text."""
    cjk = len(_CJK_RE.findall(text))
    words = len(_WORD_RE.findall(text))
    punctuation = sum(
        1
        for char in text
        if not char.isspace() and not _CJK_RE.match(char) and not char.isalnum()
    )
    return cjk + words + (punctuation + 3) // 4


def _normalized_body(capture: dict) -> str:
    candidates = [
        str(capture.get("ax_text") or "").strip(),
        str(capture.get("ocr_text") or "").strip(),
    ]
    body = max(candidates, key=lambda value: len(re.sub(r"\s+", "", value)))
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return False
    return bool(_HEADING_RE.match(stripped)) or stripped.endswith(("：", ":"))


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """Split an overlong sentence while preserving its original characters."""
    if estimate_tokens(text) <= max_tokens:
        return [text.strip()]

    pieces: list[str] = []
    start = 0
    token_count = 0
    inside_ascii_word = False
    for index, char in enumerate(text):
        if _CJK_RE.match(char):
            token_count += 1
            inside_ascii_word = False
        elif char.isalnum() or char == "_":
            if not inside_ascii_word:
                token_count += 1
                inside_ascii_word = True
        else:
            inside_ascii_word = False
            if not char.isspace() and index % 4 == 0:
                token_count += 1

        if token_count >= max_tokens:
            part = text[start : index + 1].strip()
            if part:
                pieces.append(part)
            start = index + 1
            token_count = 0
            inside_ascii_word = False

    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces


def _units_with_headings(body: str) -> list[tuple[str, str]]:
    units: list[tuple[str, str]] = []
    heading = ""
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", body) if part.strip()]
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        if _is_heading(lines[0]):
            heading = lines.pop(0)
        text = "\n".join(lines).strip()
        if not text:
            continue
        sentences = [
            sentence.strip()
            for sentence in _SENTENCE_BOUNDARY_RE.split(text)
            if sentence.strip()
        ]
        for sentence in sentences:
            for part in _hard_split(sentence, DOCUMENT_CHUNK_TARGET_TOKENS):
                units.append((heading, part))
    return units


def _suffix_for_overlap(text: str, target_tokens: int) -> str:
    if estimate_tokens(text) <= target_tokens:
        return text
    start = len(text)
    while start > 0 and estimate_tokens(text[start:]) < target_tokens:
        start -= 1
    return text[start:].strip()


def chunk_document(body: str, title: str = "") -> list[str]:
    """Build heading-aware 420-token chunks with an approximately 64-token overlap."""
    units = _units_with_headings(body)
    if not units:
        return []

    chunks: list[str] = []
    current_parts: list[str] = []
    current_heading = ""

    def render(parts: list[str], heading: str) -> str:
        prefix = []
        if title:
            prefix.append(f"文档：{title}")
        if heading and heading != title:
            prefix.append(f"章节：{heading}")
        prefix.append("\n".join(parts))
        return "\n".join(item for item in prefix if item).strip()

    for heading, unit in units:
        candidate_parts = [*current_parts, unit]
        candidate = render(candidate_parts, heading or current_heading)
        if current_parts and estimate_tokens(candidate) > DOCUMENT_CHUNK_TARGET_TOKENS:
            completed = render(current_parts, current_heading)
            chunks.append(completed)
            overlap = _suffix_for_overlap(
                "\n".join(current_parts),
                DOCUMENT_CHUNK_OVERLAP_TOKENS,
            )
            current_parts = [overlap, unit] if overlap else [unit]
            current_heading = heading or current_heading
        else:
            current_parts = candidate_parts
            current_heading = heading or current_heading

        if len(chunks) >= MAX_DOCUMENT_CHUNKS:
            break

    if current_parts and len(chunks) < MAX_DOCUMENT_CHUNKS:
        chunks.append(render(current_parts, current_heading))
    return [chunk for chunk in chunks if chunk]


def build_document_snapshot(capture: dict) -> Optional[DocumentSnapshot]:
    canonical_url = canonicalize_document_url(capture.get("url"))
    if not canonical_url:
        return None
    body = _normalized_body(capture)
    if len(re.sub(r"\s+", "", body)) < MIN_DOCUMENT_CHARS:
        return None
    title = str(
        capture.get("webpage_title")
        or capture.get("window_title")
        or capture.get("win_title")
        or "未命名文档"
    ).strip()
    if is_document_shell(body):
        return None
    chunks = chunk_document(body, title=title)
    if not chunks:
        return None
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return DocumentSnapshot(
        capture_id=int(capture.get("id") or 0),
        canonical_url=canonical_url,
        doc_key=document_doc_key(canonical_url),
        title=title,
        body=body,
        content_hash=content_hash,
        chunks=chunks,
    )


def _flatten_section_text(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [
            text
            for item in value
            for text in _flatten_section_text(item)
        ]
    if not isinstance(value, dict):
        return []

    parts: list[str] = []
    for key in ("title", "heading", "name", "content", "text", "body"):
        if key in value:
            parts.extend(_flatten_section_text(value[key]))
    for key in ("sections", "children", "items"):
        if key in value:
            parts.extend(_flatten_section_text(value[key]))
    return parts


def build_bake_document_snapshot(document: dict) -> Optional[BakeDocumentSnapshot]:
    """Build a durable vector snapshot directly from ``bake_documents``.

    Unlike capture snapshots, this source survives capture retention cleanup and
    is therefore suitable as the owner of long-lived document vectors.
    """
    document_id = int(document.get("id") or 0)
    if document_id <= 0:
        return None
    title = str(document.get("title") or "未命名文档").strip()
    full_content = str(document.get("full_content") or "").strip()
    # Derived sections must not mask a known failed source page, including
    # historical records restored without an applied source head.
    if is_document_shell(full_content):
        return None
    section_parts: list[str] = []
    sections_json = document.get("sections_json")
    if sections_json:
        try:
            parsed_sections = (
                json.loads(sections_json)
                if isinstance(sections_json, str)
                else sections_json
            )
            section_parts = _flatten_section_text(parsed_sections)
        except (TypeError, ValueError):
            section_parts = []

    body_candidates = [full_content, "\n\n".join(section_parts)]
    body = max(body_candidates, key=lambda value: len(re.sub(r"\s+", "", value)))
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body or (not document.get("source_snapshot_id") and len(re.sub(r"\s+", "", body)) < MIN_DOCUMENT_CHARS):
        return None

    canonical_url = _canonicalize_url(document.get("source_url"))
    doc_key = (
        document_doc_key(canonical_url)
        if canonical_url
        else f"bake_document:{document_id}"
    )
    if is_document_shell(body):
        return None
    chunks = chunk_document(body, title=title)
    if not chunks:
        return None
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return BakeDocumentSnapshot(
        document_id=document_id,
        canonical_url=canonical_url,
        doc_key=doc_key,
        title=title,
        body=body,
        content_hash=content_hash,
        chunks=chunks,
    )
