"""Read current source identity only when it describes the current document body."""
import sqlite3


def source_snapshot_select(conn: sqlite3.Connection, owner: str = "d", field: str = "id") -> str:
    if owner not in {"d", "bake_documents"} or field not in {"id", "completeness_status"}:
        raise ValueError("Unsupported source projection")
    required = {
        "bake_document_source_heads": {"document_id", "snapshot_id"},
        "bake_document_source_snapshots": {"id", "document_id", "identity_match", "completeness_status", "content_text"},
    }
    for table, columns in required.items():
        actual = {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}
        if not columns.issubset(actual):
            return "NULL"
    return (
        "(SELECT s." + field + " FROM bake_document_source_heads h "
        "JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id "
        "AND s.document_id=h.document_id WHERE h.document_id=" + owner + ".id "
        "AND s.identity_match=1 AND s.completeness_status='complete' "
        "AND s.content_text=" + owner + ".full_content)"
    )


def source_summary_select(conn: sqlite3.Connection, owner: str = "d") -> str:
    """Do not attach an unversioned summary to an authoritative source head."""
    if owner not in {"d", "bake_documents"}:
        raise ValueError("Unsupported summary projection")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(bake_documents)")}
    legacy_summary = owner + ".summary"
    if "summary_source_snapshot_id" in columns:
        legacy_summary = ("CASE WHEN " + owner + ".summary_source_snapshot_id IS NULL THEN "
                          + owner + ".summary ELSE NULL END")
    head_columns = {row[1] for row in conn.execute("PRAGMA table_info(bake_document_source_heads)")}
    if not {"document_id", "snapshot_id"}.issubset(head_columns):
        return legacy_summary
    binding = "0"
    if "summary_source_snapshot_id" in columns:
        binding = ("typeof(" + owner + ".summary_source_snapshot_id)='integer' AND "
                   + owner + ".summary_source_snapshot_id=" + source_snapshot_select(conn, owner))
    return ("CASE WHEN NOT EXISTS(SELECT 1 FROM bake_document_source_heads h WHERE h.document_id="
            + owner + ".id) THEN (" + legacy_summary + ") WHEN (" + binding + ") THEN "
            + owner + ".summary ELSE NULL END")
