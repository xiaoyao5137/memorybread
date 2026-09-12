ALTER TABLE bake_retry_state ADD COLUMN automatic_document_pause_bucket INTEGER
    CHECK(automatic_document_pause_bucket IS NULL OR automatic_document_pause_bucket BETWEEN 0 AND 99);
