ALTER TABLE bake_candidate_audits ADD COLUMN document_source_identity_hash TEXT
    CHECK(document_source_identity_hash IS NULL OR
        (length(document_source_identity_hash)=64 AND document_source_identity_hash NOT GLOB '*[^0-9a-f]*'));
