"""Back up and soft-delete an auto-created document proven to use another tab's body.

Dry run by default. The gate is intentionally narrow: the document must be an
active llm_bake record, and a capture carrying the same URL must expose a
different document title at the beginning of its AX text.
The generated document heading must match that conflicting title.
"""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import time


def _normalize_title(value):
    title = re.sub(r"^\s*#+\s*", "", str(value or "").strip())
    indexes = [
        title.find(separator)
        for separator in (" - ", " — ", " | ", " · ")
        if separator in title
    ]
    if indexes:
        title = title[:min(indexes)]
    return "".join(char.lower() for char in title if char.isalnum())


def _leading_document_title(text):
    prefix = str(text or "")[:240]
    lowered = prefix.lower()
    boundaries = [index for index in (
        prefix.find("\n"), lowered.find("you need to enable javascript")
    ) if index >= 0]
    if not boundaries:
        return None
    candidate = prefix[:min(boundaries)].strip()
    return candidate if 2 <= len(candidate) <= 120 else None


def _document_heading(text):
    match = re.search(r"^\s*#+\s*(.+?)\s*$", str(text or ""), re.MULTILINE)
    return match.group(1).strip() if match else None


def _table_rows(conn, table, column, identifier):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not exists:
        return []
    return [dict(row) for row in conn.execute(
        "SELECT * FROM %s WHERE %s=?" % (table, column), (identifier,)
    )]


def inspect(conn, document_id):
    document = conn.execute(
        "SELECT * FROM bake_documents WHERE id=?", (document_id,)
    ).fetchone()
    if document is None:
        raise ValueError("Document %s does not exist" % document_id)
    document = dict(document)
    if document.get("deleted_at") is not None:
        raise ValueError("Document %s is already deleted" % document_id)
    if document.get("creation_mode") != "llm_bake":
        raise ValueError("Document %s is not an auto-created bake document" % document_id)
    source_url = str(document.get("source_url") or "").strip()
    if not source_url:
        raise ValueError("Document %s does not have a source URL" % document_id)
    heading_identity = _normalize_title(_document_heading(document.get("full_content")))
    if not heading_identity:
        raise ValueError("Document %s has no generated heading to verify" % document_id)

    conflicts = []
    for row in conn.execute(
        "SELECT id,ts,webpage_title,win_title,ax_text FROM captures "
        "WHERE TRIM(COALESCE(url,''))=? ORDER BY ts,id",
        (source_url,),
    ):
        row = dict(row)
        embedded_title = _leading_document_title(row.get("ax_text"))
        if not embedded_title:
            continue
        embedded_identity = _normalize_title(embedded_title)
        page_identity = _normalize_title(row.get("webpage_title"))
        if (embedded_identity and page_identity and embedded_identity != page_identity
                and embedded_identity == heading_identity):
            conflicts.append({
                "capture_id": row["id"],
                "capture_ts": row["ts"],
                "page_title": row.get("webpage_title"),
                "embedded_document_title": embedded_title,
            })
    if not conflicts:
        raise ValueError("Document %s has no verified cross-tab source conflict" % document_id)
    return document, conflicts


def quarantine(db_path, document_id, backup_root, apply=False):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        document, conflicts = inspect(conn, document_id)
        result = {
            "document_id": document_id,
            "conflicting_capture_ids": [item["capture_id"] for item in conflicts],
            "applied": False,
        }
        if not apply:
            return result

        stamp = int(time.time() * 1000)
        folder = Path(backup_root) / ("document-source-mismatch-%s-%s" % (document_id, stamp))
        folder.mkdir(parents=True, exist_ok=False)
        backup = {
            "document": document,
            "conflicts": conflicts,
            "favorites": _table_rows(conn, "memory_favorites", "resource_id", document_id),
            "refresh_observations": _table_rows(
                conn, "bake_document_refresh_observations", "document_id", document_id
            ),
            "source_snapshots": _table_rows(
                conn, "bake_document_source_snapshots", "document_id", document_id
            ),
            "source_heads": _table_rows(
                conn, "bake_document_source_heads", "document_id", document_id
            ),
            "body_versions": _table_rows(
                conn, "bake_document_body_versions", "document_id", document_id
            ),
            "vector_index": _table_rows(
                conn, "artifact_vector_index", "document_id", document_id
            ),
        }
        (folder / "backup.json").write_text(
            json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        conn.execute(
            "UPDATE bake_documents SET deleted_at=?, updated_at=? WHERE id=? AND deleted_at IS NULL",
            (stamp, stamp, document_id),
        )
        conn.execute(
            "DELETE FROM memory_favorites WHERE resource_kind='document' AND resource_id=?",
            (document_id,),
        )
        restore = (
            "BEGIN IMMEDIATE;\n"
            "UPDATE bake_documents SET deleted_at=NULL, updated_at=%d "
            "WHERE id=%d AND deleted_at=%d;\n"
        ) % (document["updated_at"], document_id, stamp)
        for favorite in backup["favorites"]:
            if favorite.get("resource_kind") == "document":
                restore += (
                    "INSERT OR IGNORE INTO memory_favorites VALUES "
                    "('document', %d, %d, %d);\n"
                ) % (
                    favorite["resource_id"], favorite["created_at"], favorite["updated_at"]
                )
        restore += "COMMIT;\n"
        (folder / "restore.sql").write_text(restore, encoding="utf-8")
        conn.commit()
        result.update({"applied": True, "backup": str(folder)})
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--document-id", type=int, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(quarantine(
        args.db, args.document_id, args.backup_root, args.apply
    ), ensure_ascii=False))
