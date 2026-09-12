"""Back up and soft-delete explicitly selected, verified navigation-shell artifacts.

Dry run by default. Original captures, history and vector payloads are retained;
RAG resolution rejects deleted artifacts and their legacy URL vectors.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from embedding.document_quality import is_document_shell


def quarantine(db_path, document_ids, backup_root, apply=False):
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        documents = []
        for identifier in document_ids:
            row = conn.execute('SELECT * FROM bake_documents WHERE id=?', (identifier,)).fetchone()
            if row is None or row['deleted_at'] is not None:
                continue
            if not is_document_shell(str(row['full_content'] or '')):
                raise ValueError('Document %s does not meet the shell gate' % identifier)
            documents.append(dict(row))
        if not apply or not documents:
            return {'candidates': [row['id'] for row in documents], 'applied': False}
        stamp = int(time.time() * 1000)
        folder = Path(backup_root) / ('document-shells-%s' % stamp)
        folder.mkdir(parents=True, exist_ok=False)
        favorites = []
        for row in documents:
            favorites.extend(dict(item) for item in conn.execute(
                "SELECT * FROM memory_favorites WHERE resource_kind='document' AND resource_id=?", (row['id'],)))
        (folder / 'backup.json').write_text(json.dumps({'documents': documents, 'favorites': favorites}, ensure_ascii=False, indent=2))
        restore = ['BEGIN IMMEDIATE;']
        for row in documents:
            conn.execute('UPDATE bake_documents SET deleted_at=?, updated_at=? WHERE id=?', (stamp, stamp, row['id']))
            conn.execute("DELETE FROM memory_favorites WHERE resource_kind='document' AND resource_id=?", (row['id'],))
            restore.append('UPDATE bake_documents SET deleted_at=NULL, updated_at=%d WHERE id=%d AND deleted_at=%d;' % (row['updated_at'], row['id'], stamp))
        for favorite in favorites:
            restore.append("INSERT OR IGNORE INTO memory_favorites VALUES ('document', %d, %d, %d);" % (favorite['resource_id'], favorite['created_at'], favorite['updated_at']))
        restore.append('COMMIT;')
        (folder / 'restore.sql').write_text('\n'.join(restore)+'\n')
        conn.commit()
        return {'candidates': [row['id'] for row in documents], 'applied': True, 'backup': str(folder)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--document-id', type=int, action='append', required=True)
    parser.add_argument('--backup-root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(quarantine(args.db, args.document_id, args.backup_root, args.apply), ensure_ascii=False))
