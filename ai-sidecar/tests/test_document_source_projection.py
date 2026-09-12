import sqlite3

import pytest

from embedding.document_source import source_snapshot_select, source_summary_select


@pytest.mark.parametrize("change", [
    "UPDATE bake_documents SET full_content='edited'",
    "UPDATE bake_document_source_snapshots SET document_id=2",
    "UPDATE bake_document_source_snapshots SET identity_match=0",
    "UPDATE bake_document_source_snapshots SET completeness_status='partial'",
    "DELETE FROM bake_document_source_snapshots",
])
def test_source_projection_rejects_stale_or_unproven_head(change):
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE bake_documents(id INTEGER,full_content TEXT);
        CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER);
        CREATE TABLE bake_document_source_snapshots(id INTEGER,document_id INTEGER,content_text TEXT,identity_match INTEGER,completeness_status TEXT);
        INSERT INTO bake_documents VALUES(1,'body');
        INSERT INTO bake_document_source_heads VALUES(1,7);
        INSERT INTO bake_document_source_snapshots VALUES(7,1,'body',1,'complete');
    """)
    sql = "SELECT " + source_snapshot_select(conn) + " FROM bake_documents d"
    assert conn.execute(sql).fetchone()[0] == 7
    conn.execute(change)
    assert conn.execute(sql).fetchone()[0] is None


def test_missing_source_schema_is_unverified_and_projection_names_are_fixed():
    conn = sqlite3.connect(":memory:")
    assert source_snapshot_select(conn) == "NULL"
    with pytest.raises(ValueError):
        source_snapshot_select(conn, "arbitrary alias")


def test_summary_projection_requires_current_valid_source_binding():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE bake_documents(id INTEGER, full_content TEXT, summary TEXT);
        INSERT INTO bake_documents VALUES(1,'current body','legacy summary');
    """)
    def summary():
        return conn.execute("SELECT " + source_summary_select(conn) + " FROM bake_documents d").fetchone()[0]
    assert summary() == "legacy summary"
    conn.executescript("""
        CREATE TABLE bake_document_source_heads(document_id INTEGER,snapshot_id INTEGER);
        CREATE TABLE bake_document_source_snapshots(id INTEGER,document_id INTEGER,content_text TEXT,identity_match INTEGER,completeness_status TEXT);
        INSERT INTO bake_document_source_heads VALUES(1,7);
        INSERT INTO bake_document_source_snapshots VALUES(7,1,'current body',1,'complete');
    """)
    assert summary() is None
    conn.execute("ALTER TABLE bake_documents ADD COLUMN summary_source_snapshot_id INTEGER")
    conn.execute("UPDATE bake_documents SET summary_source_snapshot_id=6")
    assert summary() is None
    conn.execute("UPDATE bake_documents SET summary_source_snapshot_id=7")
    assert summary() == "legacy summary"
    conn.execute("UPDATE bake_documents SET full_content='changed body'")
    assert summary() is None
    conn.execute("DELETE FROM bake_document_source_heads")
    assert summary() is None
    with pytest.raises(ValueError):
        source_summary_select(conn, "untrusted alias")
