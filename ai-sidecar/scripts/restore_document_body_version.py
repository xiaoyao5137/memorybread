#!/usr/bin/env python3
"""Preview or restore an archived document body; keep sources and history intact."""
import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from embedding.document_quality import is_document_shell

BODY_FIELDS = ('title', 'full_content', 'summary', 'structured_content', 'sections_json',
               'tags', 'prompt_hint', 'content_hash', 'generation_version', 'evidence_summary',
               'style_phrases', 'replacement_rules', 'applicable_tasks', 'diagram_code',
               'image_assets', 'language', 'match_score', 'match_level')
JSON_DEFAULTS = {'structured_content': '{}', 'sections_json': '[]', 'tags': '[]',
                 'style_phrases': '[]', 'replacement_rules': '[]',
                 'applicable_tasks': '[]', 'image_assets': '[]'}


def _current_state(conn, document_id):
    current = conn.execute('SELECT * FROM bake_documents WHERE id=? AND deleted_at IS NULL', (document_id,)).fetchone()
    if current is None:
        raise ValueError('active document not found')
    result = dict(current)
    head = conn.execute('SELECT * FROM bake_document_source_heads WHERE document_id=?', (document_id,)).fetchone()
    result['_source_head'] = dict(head) if head else None
    return result


def _state_digest(state):
    return hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _write_private_json(path, value):
    # Create with restricted permissions before writing any content.
    import os
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def restore(db_path, version_id=None, apply=False, source_snapshot_id=None, undo_manifest=None):
    path = Path(db_path).resolve()
    conn = sqlite3.connect('file:' + str(path) + ('?mode=rw' if apply else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        if undo_manifest is not None:
            if version_id is not None or source_snapshot_id is not None:
                raise ValueError('undo manifest cannot be combined with version or snapshot')
            manifest_path = Path(undo_manifest).resolve()
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            if manifest.get('schema_version') != 'document-restore-receipt.v1' or manifest.get('database') != str(path):
                raise ValueError('restore receipt database or schema mismatch')
            backup_path = manifest_path.parent / manifest['backup_file']
            if backup_path.resolve().parent != manifest_path.parent:
                raise ValueError('backup must be next to receipt')
            raw = backup_path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != manifest['backup_sha256']:
                raise ValueError('restore backup checksum mismatch')
            old = json.loads(raw)
            row = {'document_id': manifest['document_id']}
            if old.get('id') != row['document_id']:
                raise ValueError('restore backup document mismatch')
            if _state_digest(_current_state(conn, row['document_id'])) != manifest['after_state_sha256']:
                raise ValueError('document changed since restore; refusing undo')
            previous_head = old.get('_source_head')
            source_snapshot_id = previous_head['snapshot_id'] if previous_head else None
        else:
            row = conn.execute('SELECT * FROM bake_document_body_versions WHERE id=?', (version_id,)).fetchone()
            if row is None:
                raise ValueError('version not found')
            old = json.loads(row['record_json'])
        report = {'document_id': row['document_id'], 'version_id': version_id, 'applied': False}
        snapshot = None
        if source_snapshot_id is not None:
            try:
                if type(source_snapshot_id) is not int or source_snapshot_id <= 0:
                    raise ValueError('invalid source snapshot id')
                current_source = conn.execute('SELECT source_url FROM bake_documents WHERE id=? AND deleted_at IS NULL', (row['document_id'],)).fetchone()
                snapshot = conn.execute('SELECT * FROM bake_document_source_snapshots WHERE id=?', (source_snapshot_id,)).fetchone()
                body = old.get('full_content') or ''
                if (snapshot is None or current_source is None
                        or snapshot['document_id'] != row['document_id']
                        or snapshot['identity_match'] != 1
                        or snapshot['completeness_status'] != 'complete'
                        or snapshot['truncated'] != 0
                        or not body.strip() or is_document_shell(body)
                        or snapshot['content_text'] != body
                        or snapshot['content_hash'] != hashlib.sha256(body.encode('utf-8')).hexdigest()
                        or not current_source['source_url']
                        or snapshot['source_url'] != current_source['source_url']):
                    raise ValueError('source snapshot does not verify the archived body')
            except Exception:
                raise
            report['source_snapshot_id'] = source_snapshot_id
        if apply:
            current = conn.execute('SELECT * FROM bake_documents WHERE id=? AND deleted_at IS NULL', (row['document_id'],)).fetchone()
            if current is None:
                raise ValueError('active document not found')
            backup_dir = path.parent / 'repair-backups'
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / ('document-before-restore-%s-%s.json' % (row['document_id'], time.time_ns()))
            before = dict(current)
            previous_head = conn.execute('SELECT * FROM bake_document_source_heads WHERE document_id=?', (row['document_id'],)).fetchone()
            before['_source_head'] = dict(previous_head) if previous_head else None
            _write_private_json(backup, before)
            backup_sha256 = hashlib.sha256(backup.read_bytes()).hexdigest()
            # Field names are a fixed local allowlist, never user-supplied SQL.
            columns = {item[1] for item in conn.execute('PRAGMA table_info(bake_documents)')}
            fields = [name for name in BODY_FIELDS if name in columns]
            # Restored historical text is unverified; never retain a newer summary binding.
            bindings = [name for name in ('summary_source_snapshot_id', 'summary_generation_version')
                        if name in columns]
            assignments = [name + '=?' for name in fields] + [name + '=NULL' for name in bindings]
            conn.execute('UPDATE bake_documents SET ' + ','.join(assignments)
                         + ",updated_at=?,last_refresh_status='historical_only',last_refresh_completeness='unverified' WHERE id=?",
                         [old.get(name, JSON_DEFAULTS.get(name)) for name in fields]
                         + [int(time.time() * 1000), row['document_id']])
            conn.execute('DELETE FROM bake_document_source_heads WHERE document_id=?', (row['document_id'],))
            if snapshot is not None:
                conn.execute('INSERT INTO bake_document_source_heads(document_id,snapshot_id,applied_at) VALUES(?,?,?)',
                             (row['document_id'], source_snapshot_id, int(time.time() * 1000)))
                conn.execute("UPDATE bake_documents SET last_refresh_completeness='complete' WHERE id=?", (row['document_id'],))
                restored_metadata = {'content_hash': snapshot['content_hash'],
                                     'last_refresh_content_hash': snapshot['content_hash'],
                                     'last_refresh_truncated': 0}
                for target, source in [('last_refresh_character_count', 'character_count'),
                                       ('last_refresh_segment_count', 'segment_count'),
                                       ('last_refresh_success_at_ms', 'collected_at')]:
                    if source in snapshot.keys():
                        restored_metadata[target] = snapshot[source]
                restored_metadata = {key: value for key, value in restored_metadata.items() if key in columns}
                if restored_metadata:
                    conn.execute('UPDATE bake_documents SET ' + ','.join(key + '=?' for key in restored_metadata) + ' WHERE id=?',
                                 list(restored_metadata.values()) + [row['document_id']])
                # Only the archived binding can authorize the archived summary.
                if (len(bindings) == 2 and type(old.get('summary_source_snapshot_id')) is int
                        and old['summary_source_snapshot_id'] == source_snapshot_id
                        and old.get('summary_generation_version')):
                    conn.execute('UPDATE bake_documents SET summary_source_snapshot_id=?,summary_generation_version=? WHERE id=?',
                                 (source_snapshot_id, old['summary_generation_version'], row['document_id']))
            tables = {item[0] for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'artifact_vector_index' in tables:
                if 'vector_deletion_queue' in tables:
                    conn.execute("INSERT OR IGNORE INTO vector_deletion_queue "
                                 "(qdrant_point_id,source_type,reason,enqueued_at) "
                                 "SELECT qdrant_point_id,'document','document_body_restored',? "
                                 "FROM artifact_vector_index WHERE document_id=?",
                                 (int(time.time() * 1000), row['document_id']))
                conn.execute('DELETE FROM artifact_vector_index WHERE document_id=?', (row['document_id'],))
            after = _current_state(conn, row['document_id'])
            receipt_path = backup.with_name(backup.stem + '.receipt.json')
            receipt = {'schema_version': 'document-restore-receipt.v1',
                       'database': str(path), 'document_id': row['document_id'],
                       'version_id': version_id, 'source_snapshot_id': source_snapshot_id,
                       'undo_of': str(Path(undo_manifest).resolve()) if undo_manifest else None,
                       'backup_file': backup.name, 'backup_sha256': backup_sha256,
                       'before_state_sha256': _state_digest(before),
                       'after_state_sha256': _state_digest(after),
                       'state': 'prepared',
                       'reverse_argv': [sys.executable, str(Path(__file__).resolve()),
                                        '--db', str(path), '--undo-manifest', str(receipt_path), '--apply'],
                       'index_action': 'invalidate_and_rebuild'}
            # A prepared receipt is durable before commit. It alone is not proof
            # that commit succeeded; undo checks the actual current state.
            _write_private_json(receipt_path, receipt)
            conn.commit()
            report.update(applied=True, backup=str(backup), manifest=str(receipt_path),
                          backup_sha256=backup_sha256, after_state_sha256=receipt['after_state_sha256'])
        return report
    finally:
        conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--version-id', type=int)
    group.add_argument('--undo-manifest')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--source-snapshot-id', type=int, help='Explicit verified snapshot matching the archived body; never inferred')
    args = parser.parse_args()
    print(json.dumps(restore(args.db, args.version_id, args.apply, args.source_snapshot_id, args.undo_manifest), ensure_ascii=False))
