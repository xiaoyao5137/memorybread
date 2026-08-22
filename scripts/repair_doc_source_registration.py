#!/usr/bin/env python3
"""一次性修补：历史上被 URL coalesce 跳过、漏登记进文档来源的 timeline。

口径与 core-engine 保持一致：
- canonical_document_identity: 小写、去 fragment/query/scheme/尾部斜杠，且需命中文档 URL 标记
- 合并字段: source_memory_ids / source_episode_ids 加 timeline id；
  source_capture_ids 加 timeline 关联的全部 capture id（primary + capture_ids + captures.timeline_id）
"""
import json
import sqlite3
import sys
import time

DB = "/Users/xianjiaqi/.memory-bread/memory-bread.db"

MARKERS = [
    "/docs/", "docs.google", "/document/", "yuque.com", "feishu.cn/docx",
    "feishu.cn/wiki", "larkoffice.com/wiki", "notion.so", "confluence",
    "/wiki/", "shimo.im", "/d/home/", "/s/home/", "/k/home/",
]

MISSING = [
    5110, 5112, 5124, 5134, 5191, 5354, 5375, 5400, 5415, 5428,
    5462, 5465, 5468, 5487, 5509, 5515, 5523, 5525, 5535, 5537,
    5545, 5555, 5565, 5590, 5614, 5616, 5618, 5619, 5622, 5683,
    5692, 5696, 5701, 5705, 5708, 5718, 5722, 5727, 5796, 5833,
    5838, 5842, 5872, 5904, 5973, 5997, 6000, 6002, 6003, 6004,
    6005, 6006, 6007, 6009, 6278,
]


def canonical_identity(url):
    trimmed = (url or "").strip()
    if not trimmed:
        return None
    lowered = trimmed.lower()
    if not any(m in lowered for m in MARKERS):
        return None
    no_frag = trimmed.split("#", 1)[0]
    no_query = no_frag.split("?", 1)[0]
    for prefix in ("https://", "http://"):
        if no_query.startswith(prefix):
            no_query = no_query[len(prefix):]
            break
    identity = no_query.rstrip("/").strip().lower()
    return identity or None


def parse_ids(raw):
    try:
        value = json.loads(raw or "[]")
        return [str(v) for v in value] if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 文档索引：identity -> (doc_id, source_memory_ids, source_capture_ids, source_episode_ids)
    docs = conn.execute(
        "SELECT id, source_url, document_identity, source_memory_ids,"
        " source_capture_ids, source_episode_ids FROM bake_documents"
        " WHERE deleted_at IS NULL AND source_url IS NOT NULL"
        " ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    doc_by_identity = {}
    for d in docs:
        ident = d["document_identity"] or canonical_identity(d["source_url"] or "")
        if ident and ident not in doc_by_identity:
            doc_by_identity[ident] = dict(d)

    repairs = {}   # doc_id -> {"memory": set, "capture": set, "episode": set, "timelines": []}
    skipped = []

    for tid in MISSING:
        row = conn.execute(
            "SELECT capture_id, capture_ids FROM timelines WHERE id = ?", (tid,)
        ).fetchone()
        if row is None:
            skipped.append((tid, "timeline 不存在"))
            continue
        capture_ids = {row["capture_id"]}
        capture_ids.update(int(v) for v in parse_ids(row["capture_ids"]) if str(v).isdigit())
        linked = conn.execute(
            "SELECT id, url FROM captures WHERE timeline_id = ?", (tid,)
        ).fetchall()
        urls = [c["url"] for c in linked if c["url"]]
        capture_ids.update(c["id"] for c in linked)
        ident = None
        for url in urls:
            ident = canonical_identity(url)
            if ident:
                break
        if not ident:
            skipped.append((tid, "无有效文档 URL"))
            continue
        doc = doc_by_identity.get(ident)
        if doc is None:
            skipped.append((tid, "无匹配文档 identity=%s" % ident))
            continue
        entry = repairs.setdefault(doc["id"], {
            "memory": set(parse_ids(doc["source_memory_ids"])),
            "capture": set(parse_ids(doc["source_capture_ids"])),
            "episode": set(parse_ids(doc["source_episode_ids"])),
            "timelines": [],
        })
        entry["memory"].add(str(tid))
        entry["episode"].add(str(tid))
        entry["capture"].update(str(c) for c in capture_ids)
        entry["timelines"].append(tid)

    now_ms = int(time.time() * 1000)
    for doc_id, entry in repairs.items():
        conn.execute(
            "UPDATE bake_documents SET source_memory_ids = ?, source_capture_ids = ?,"
            " source_episode_ids = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (
                json.dumps(sorted(entry["memory"], key=int)),
                json.dumps(sorted(entry["capture"], key=int)),
                json.dumps(sorted(entry["episode"], key=int)),
                now_ms,
                doc_id,
            ),
        )
        print("doc %d <- timelines %s" % (doc_id, entry["timelines"]))
    conn.commit()

    for tid, reason in skipped:
        print("skip timeline %d: %s" % (tid, reason))
    print("repaired docs: %d, skipped timelines: %d" % (len(repairs), len(skipped)))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
