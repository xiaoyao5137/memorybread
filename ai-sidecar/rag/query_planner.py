"""Corpus-aware lexical query planning for durable artifacts.

The planner separates terms that identify *which* artifact the user means from
artifact-type words and conversational instructions.  Only discriminative
terms generate candidates when they are available, so common words such as
``文档`` or ``资料`` cannot crowd a rare identifier out of the candidate set.
"""

from __future__ import annotations

import math
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

_MAX_STAT_TERMS = 16
_MAX_DISCRIMINATIVE_TERMS = 8
_MAX_FALLBACK_TERMS = 3
_DYNAMIC_GENERIC_MIN_CORPUS = 20
_DYNAMIC_GENERIC_MIN_DOCUMENTS = 8
_DYNAMIC_GENERIC_RATIO = 0.18
_DF_CACHE_TTL_SECS = 5 * 60
_DF_CACHE_MAX_ENTRIES = 512

_df_cache: OrderedDict[tuple[str, str], tuple[float, int, int]] = OrderedDict()
_df_cache_lock = threading.Lock()

_INSTRUCTION_TERMS = (
    "帮我查一下",
    "帮我找一下",
    "帮我",
    "请帮忙",
    "请问",
    "查一下",
    "找一下",
    "搜一下",
    "梳理一下",
    "整理一下",
    "总结一下",
    "给我",
    "我想找",
    "我想看",
    "查找",
    "查询",
    "搜索",
    "召回",
    "找到",
    "看看",
    "梳理",
    "整理",
    "总结",
    "介绍",
    "说明",
    "相关的",
    "相关",
    "有关",
    "关于",
    "一下",
    "为什么",
    "为何",
    "怎么",
    "如何",
    "是否",
    "哪些",
    "什么",
)

_TYPE_TERM_SOURCES: dict[str, frozenset[str]] = {
    "技术文档": frozenset({"document"}),
    "设计文档": frozenset({"document"}),
    "文档": frozenset({"document"}),
    "资料": frozenset({"document"}),
    "报告": frozenset({"document"}),
    "方案": frozenset({"document"}),
    "文章": frozenset({"document"}),
    "网页": frozenset({"document"}),
    "页面": frozenset({"document"}),
    "链接": frozenset({"document"}),
    "地址": frozenset({"document"}),
    "网址": frozenset({"document"}),
    "document": frozenset({"document"}),
    "doc": frozenset({"document"}),
    "pdf": frozenset({"document"}),
    "知识点": frozenset({"knowledge"}),
    "知识": frozenset({"knowledge"}),
    "结论": frozenset({"knowledge"}),
    "操作流程": frozenset({"operation"}),
    "操作步骤": frozenset({"operation"}),
    "操作": frozenset({"operation"}),
    "流程": frozenset({"operation"}),
    "步骤": frozenset({"operation"}),
    "教程": frozenset({"operation"}),
    "指南": frozenset({"operation"}),
    "手册": frozenset({"operation"}),
    "sop": frozenset({"operation"}),
    "数据记忆": frozenset({"data"}),
    "数据": frozenset({"data"}),
    "data": frozenset({"data"}),
}

_BOUNDARY_PARTICLES = frozenset("的了着过是在于和与及或把被给向从对中里上下一些这那")
_ASCII_TERM_RE = re.compile(r"[a-z][a-z0-9._:/+-]*|\d+[a-z0-9._:/+-]+", re.IGNORECASE)
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

_ARTIFACT_TABLES: dict[str, tuple[tuple[str, ...], str]] = {
    "bake_documents": (
        ("title", "doc_type", "summary", "full_content", "sections_json", "source_url"),
        "deleted_at IS NULL",
    ),
    "bake_knowledge": (
        ("title", "summary", "content", "detailed_content", "entities"),
        "1=1",
    ),
    "bake_sops": (
        ("title", "summary", "content", "detailed_content", "entities"),
        "1=1",
    ),
    "data_sources": (
        ("title", "source_url", "tags", "source_window_title"),
        "deleted_at IS NULL",
    ),
    "data_snapshots": (
        ("content_text", "structured_data"),
        "status IN ('success', 'partial')",
    ),
}


@dataclass(frozen=True)
class PlannedTerm:
    text: str
    role: str
    document_frequency: int
    idf: float


@dataclass(frozen=True)
class ArtifactQueryPlan:
    discriminative_terms: tuple[PlannedTerm, ...]
    type_terms: tuple[PlannedTerm, ...]
    generic_terms: tuple[PlannedTerm, ...]
    instruction_terms: tuple[str, ...]
    source_types: frozenset[str]
    corpus_size: int
    fallback_terms: tuple[PlannedTerm, ...] = ()

    @property
    def candidate_terms(self) -> list[str]:
        selected = self.discriminative_terms or self.fallback_terms
        return [term.text for term in selected]

    @property
    def ranking_terms(self) -> list[str]:
        selected = [*self.discriminative_terms, *self.fallback_terms]
        return list(dict.fromkeys(term.text for term in selected))

    def weight_for(self, term: str) -> float:
        lowered = term.lower()
        for planned in (*self.discriminative_terms, *self.fallback_terms):
            if planned.text == lowered:
                return planned.idf
        return 0.15


def _contains_phrase(text: str, phrase: str) -> bool:
    if phrase.isascii() and phrase.isalnum():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text))
    return phrase in text


def _remove_phrase(text: str, phrase: str) -> str:
    if phrase.isascii() and phrase.isalnum():
        return re.sub(
            rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
            " ",
            text,
        )
    return text.replace(phrase, " ")


def _valid_cjk_candidate(value: str) -> bool:
    return (
        len(value) >= 2
        and value[0] not in _BOUNDARY_PARTICLES
        and value[-1] not in _BOUNDARY_PARTICLES
    )


def _surface_terms(
    query: str,
    entity_terms: Optional[list[str]],
) -> tuple[list[str], list[str], list[str], frozenset[str]]:
    lowered = query.lower()
    working = lowered
    instructions: list[str] = []
    type_terms: list[str] = []
    source_types: set[str] = set()

    for phrase in _INSTRUCTION_TERMS:
        if _contains_phrase(working, phrase):
            instructions.append(phrase)
            working = _remove_phrase(working, phrase)

    # Longest terms run first so “技术文档” is not reduced to a dangling “技术”.
    for phrase in sorted(_TYPE_TERM_SOURCES, key=len, reverse=True):
        if phrase == "数据" and "数据库" in working:
            continue
        if _contains_phrase(working, phrase):
            type_terms.append(phrase)
            source_types.update(_TYPE_TERM_SOURCES[phrase])
            working = _remove_phrase(working, phrase)

    candidates: list[str] = []
    candidates.extend(str(term).strip().lower() for term in (entity_terms or []) if str(term).strip())
    candidates.extend(match.group(0).lower() for match in _ASCII_TERM_RE.finditer(working))

    for run in _CJK_RUN_RE.findall(working):
        run = run.strip("".join(_BOUNDARY_PARTICLES))
        if not _valid_cjk_candidate(run):
            continue
        pieces = [
            piece
            for piece in re.split(r"[的了在与和及或中里]+", run)
            if _valid_cjk_candidate(piece)
        ]
        for piece in pieces:
            if len(piece) <= 6:
                candidates.append(piece)
            if len(piece) < 5:
                continue
            for size in (4, 3, 2):
                for start in range(len(piece) - size + 1):
                    candidate = piece[start : start + size]
                    if _valid_cjk_candidate(candidate):
                        candidates.append(candidate)
                    if len(candidates) >= _MAX_STAT_TERMS * 2:
                        break

    normalized_candidates = list(
        dict.fromkeys(
            term
            for term in candidates
            if len(term) >= 2 and term not in _INSTRUCTION_TERMS
        )
    )
    return (
        normalized_candidates[:_MAX_STAT_TERMS],
        list(dict.fromkeys(type_terms)),
        list(dict.fromkeys(instructions)),
        frozenset(source_types),
    )


def _table_columns(cursor: sqlite3.Cursor, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in cursor.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _document_frequencies(
    cursor: sqlite3.Cursor,
    terms: list[str],
) -> tuple[int, dict[str, int]]:
    frequencies = {term: 0 for term in terms}
    corpus_size = 0
    if not terms:
        return corpus_size, frequencies

    for table, (candidate_columns, where_clause) in _ARTIFACT_TABLES.items():
        columns = _table_columns(cursor, table)
        searchable = [column for column in candidate_columns if column in columns]
        if not searchable:
            continue
        effective_where = where_clause if "deleted_at" in columns else "1=1"
        text_expression = "LOWER(" + " || ' ' || ".join(
            f"COALESCE({column}, '')" for column in searchable
        ) + ")"
        projections = [
            f"SUM(CASE WHEN {text_expression} LIKE ? THEN 1 ELSE 0 END)"
            for _ in terms
        ]
        sql = (
            f"SELECT COUNT(*), {', '.join(projections)} "
            f"FROM {table} WHERE {effective_where}"
        )
        row = cursor.execute(sql, [f"%{term}%" for term in terms]).fetchone()
        if row is None:
            continue
        corpus_size += int(row[0] or 0)
        for index, term in enumerate(terms, start=1):
            frequencies[term] += int(row[index] or 0)
    return corpus_size, frequencies


def _database_identity(cursor: sqlite3.Cursor) -> str:
    try:
        row = cursor.execute("PRAGMA database_list").fetchone()
        if row and row[2]:
            return str(row[2])
    except sqlite3.Error:
        pass
    return f"connection:{id(cursor.connection)}"


def _cached_document_frequencies(
    cursor: sqlite3.Cursor,
    terms: list[str],
) -> tuple[int, dict[str, int]]:
    if not terms:
        return 0, {}
    now = time.monotonic()
    database_identity = _database_identity(cursor)
    frequencies: dict[str, int] = {}
    corpus_sizes: list[int] = []
    missing: list[str] = []

    with _df_cache_lock:
        for term in terms:
            key = (database_identity, term)
            cached = _df_cache.get(key)
            if cached is None or cached[0] <= now:
                _df_cache.pop(key, None)
                missing.append(term)
                continue
            _df_cache.move_to_end(key)
            _, corpus_size, frequency = cached
            corpus_sizes.append(corpus_size)
            frequencies[term] = frequency

    if missing:
        corpus_size, fresh = _document_frequencies(cursor, missing)
        frequencies.update(fresh)
        corpus_sizes.append(corpus_size)
        expires_at = now + _DF_CACHE_TTL_SECS
        with _df_cache_lock:
            for term in missing:
                key = (database_identity, term)
                _df_cache[key] = (expires_at, corpus_size, fresh.get(term, 0))
                _df_cache.move_to_end(key)
            while len(_df_cache) > _DF_CACHE_MAX_ENTRIES:
                _df_cache.popitem(last=False)

    return max(corpus_sizes, default=0), frequencies


def _idf(corpus_size: int, document_frequency: int) -> float:
    return math.log((max(0, corpus_size) + 1) / (max(0, document_frequency) + 1)) + 1.0


def _prefer_non_redundant(terms: list[PlannedTerm], limit: int) -> list[PlannedTerm]:
    ordered = sorted(
        terms,
        key=lambda term: (
            term.idf,
            any(char.isascii() and char.isalnum() for char in term.text),
            len(term.text),
        ),
        reverse=True,
    )
    selected: list[PlannedTerm] = []
    for term in ordered:
        if any(
            term.text in existing.text
            and term.document_frequency == existing.document_frequency
            for existing in selected
        ):
            continue
        selected.append(term)
        if len(selected) >= limit:
            break
    return selected


def build_artifact_query_plan(
    cursor: sqlite3.Cursor,
    query: str,
    entity_terms: Optional[list[str]] = None,
) -> ArtifactQueryPlan:
    candidates, explicit_types, instructions, source_types = _surface_terms(query, entity_terms)
    corpus_size, frequencies = _cached_document_frequencies(cursor, candidates)
    entity_set = {str(term).strip().lower() for term in (entity_terms or []) if str(term).strip()}

    discriminative: list[PlannedTerm] = []
    dynamic_types: list[PlannedTerm] = []
    for term in candidates:
        document_frequency = frequencies.get(term, 0)
        if document_frequency <= 0:
            continue
        idf = _idf(corpus_size, document_frequency)
        is_dynamic_generic = (
            term not in entity_set
            and corpus_size >= _DYNAMIC_GENERIC_MIN_CORPUS
            and document_frequency >= _DYNAMIC_GENERIC_MIN_DOCUMENTS
            and document_frequency / max(1, corpus_size) >= _DYNAMIC_GENERIC_RATIO
        )
        planned = PlannedTerm(
            text=term,
            role="generic" if is_dynamic_generic else "discriminative",
            document_frequency=document_frequency,
            idf=idf,
        )
        if is_dynamic_generic:
            dynamic_types.append(planned)
        else:
            discriminative.append(planned)

    planned_types = [
        PlannedTerm(
            text=term,
            role="type",
            document_frequency=frequencies.get(term, 0),
            idf=_idf(corpus_size, frequencies.get(term, 0)),
        )
        for term in explicit_types
    ]
    planned_types.extend(dynamic_types)
    selected_discriminative = _prefer_non_redundant(
        discriminative,
        _MAX_DISCRIMINATIVE_TERMS,
    )
    fallback = []
    if not selected_discriminative:
        fallback = _prefer_non_redundant(dynamic_types, _MAX_FALLBACK_TERMS)

    return ArtifactQueryPlan(
        discriminative_terms=tuple(selected_discriminative),
        type_terms=tuple(
            {term.text: term for term in planned_types if term.role == "type"}.values()
        ),
        generic_terms=tuple(
            {term.text: term for term in dynamic_types}.values()
        ),
        instruction_terms=tuple(instructions),
        source_types=source_types,
        corpus_size=corpus_size,
        fallback_terms=tuple(fallback),
    )
