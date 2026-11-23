"""Select question-relevant passages from already recalled documents."""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Callable

from .retriever import RetrievedChunk, _phrase_present

logger = logging.getLogger(__name__)
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)\s*$")
_URL = re.compile(r"https?://[^\s<>，。；]+", re.IGNORECASE)
_LINK_QUERY_TERMS = {"url", "链接", "地址", "网址", "页面", "入口"}


@dataclass
class Section:
    title: str
    lines: list[str]


def load_document_sections(
    db_path: str, chunks: list[RetrievedChunk],
) -> dict[int, list[Section]]:
    ids = {
        chunk.metadata.get("document_id") for chunk in chunks
        if (chunk.metadata.get("source_type") or chunk.source) == "document"
        and isinstance(chunk.metadata.get("document_id"), int)
        and not chunk.metadata.get("source_snapshot_id")
    }
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            sections = {
                document_id: _split_sections(body or "")
                for document_id, body in conn.execute(
                    f"SELECT id, full_content FROM bake_documents "
                    f"WHERE deleted_at IS NULL AND id IN ({placeholders})", tuple(ids),
                )
            }
            # Legacy records can have only structured sections. When a body
            # exists, use it as the authority for the current document text.
            missing = [document_id for document_id, items in sections.items() if not items]
            if missing and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'bake_document_sections'"
            ).fetchone():
                placeholders = ",".join("?" for _ in missing)
                for document_id, title, content in conn.execute(
                    f"SELECT document_id, title, content FROM bake_document_sections "
                    f"WHERE document_id IN ({placeholders}) ORDER BY section_index, id", missing,
                ):
                    if content and content.strip():
                        sections[document_id].append(Section(title or "", content.splitlines()))
            return sections
    except sqlite3.Error:
        logger.warning("读取召回文档正文失败，使用已有片段")
        return {}


def _split_sections(body: str) -> list[Section]:
    sections: list[Section] = []
    title = ""
    lines: list[str] = []
    fence = ""
    for line in body.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            if not fence:
                fence = marker[1]
            elif marker[1].startswith(fence) and not line[marker.end():].strip():
                fence = ""
        heading = _HEADING.match(line) if not fence else None
        if heading:
            if any(line.strip() for line in lines):
                sections.append(Section(title, lines))
            title, lines = heading[1], []
        else:
            lines.append(line)
    if any(line.strip() for line in lines):
        sections.append(Section(title, lines))
    return sections


def fit_prefix(text: str, fits: Callable[[str], bool]) -> str:
    """Fit a prefix without returning part of an address."""
    if fits(text):
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(text[:mid]):
            low = mid
        else:
            high = mid - 1
    for match in _URL.finditer(text):
        if match.start() < low < match.end():
            low = match.start()
            break
        if match.start() >= low:
            break
    return text[:low].rstrip()


def select_document_text(
    chunk: RetrievedChunk,
    sections: list[Section],
    terms: list[str],
    link_query: bool,
    fits: Callable[[str], bool],
) -> str:
    if not sections:
        return ""
    terms = [term for term in terms if not link_query or term not in _LINK_QUERY_TERMS]

    def score(text: str) -> int:
        searchable = _URL.sub("", text).lower()
        return sum(len(term) for term in terms if _phrase_present(searchable, term))

    def section_rank(section: Section) -> tuple[int, int]:
        title_score = score(section.title)
        body_score = max((score(line) for line in section.lines), default=0)
        has_link = any(_URL.search(line) for line in section.lines)
        return int(link_query and has_link and bool(title_score or body_score)), 3 * title_score + body_score

    ranked = sorted(((section_rank(section), section) for section in sections),
                    key=lambda item: item[0], reverse=True)
    title = chunk.metadata.get("title") or "文档"
    source = chunk.metadata.get("source_url") or chunk.metadata.get("url")
    header = f"文档：{title}" + (f"\nURL：{source}" if source else "")
    excerpts: list[str] = []
    for rank, section in ranked:
        if excerpts and (link_query or not any(rank)):
            break
        prefix = "\n\n".join([header, *excerpts]) + "\n\n"
        heading = f"章节：{section.title}\n" if section.title else ""
        lines = section.lines
        body = "\n".join(lines).strip()
        if not (link_query and any(_URL.search(line) for line in lines)) and fits(prefix + heading + body):
            excerpts.append(heading + body)
            continue

        nonempty = [i for i, line in enumerate(lines) if line.strip()]
        anchor = max(nonempty, key=lambda i: score(lines[i]))
        selected = {anchor}
        links = [i for i in nonempty if _URL.search(lines[i])]
        if link_query and links:
            selected.add(min(links, key=lambda i: abs(i - anchor)))

        def render(indices: set[int]) -> str:
            return heading + "\n".join(lines[i] for i in sorted(indices))

        excerpt = render(selected)
        if not fits(prefix + excerpt):
            line = lines[anchor]
            lowered = line.lower()
            matched = next(
                (lowered.index(term) for term in sorted(terms, key=len, reverse=True) if term in lowered), 0,
            )
            start = max(0, matched - 48)
            if link_query and links:
                link_line = min(links, key=lambda i: abs(i - anchor))
                if anchor != link_line:
                    continue
                link = min(_URL.finditer(line), key=lambda m: abs(m.start() - matched))
                start = max(0, min(start, link.start() - 48, matched))
                if not fits(prefix + heading + line[start:link.end()]):
                    continue
            clipped = fit_prefix(line[start:], lambda part: fits(prefix + heading + part))
            if clipped:
                excerpts.append(heading + clipped)
            continue
        # Add nearby lines while keeping the heading, matching line and link
        # together. A link lookup stops before an unrelated link in the section.
        for i in sorted(nonempty, key=lambda i: abs(i - anchor)):
            if i in selected:
                continue
            if link_query and links and (i in links or abs(i - anchor) > 3):
                continue
            if not fits(prefix + render(selected | {i})):
                break
            selected.add(i)
        excerpts.append(render(selected))

    return "\n\n".join([header, *excerpts]) if excerpts else ""
