-- Repair derived indexes that predate an already applied source version.
-- Keep source records, document IDs and revision backups intact.
INSERT OR IGNORE INTO vector_deletion_queue
    (qdrant_point_id, source_type, reason, enqueued_at)
SELECT v.qdrant_point_id, 'document', 'document_source_replaced', h.applied_at
FROM artifact_vector_index v
JOIN bake_document_source_heads h ON h.document_id = v.document_id
WHERE v.indexed_at < h.applied_at;

DELETE FROM artifact_vector_index
WHERE id IN (
    SELECT v.id FROM artifact_vector_index v
    JOIN bake_document_source_heads h ON h.document_id = v.document_id
    WHERE v.indexed_at < h.applied_at
);
