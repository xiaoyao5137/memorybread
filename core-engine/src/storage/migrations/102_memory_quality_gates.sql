ALTER TABLE bake_artifact_audits ADD COLUMN decision_state TEXT;
ALTER TABLE bake_artifact_audits ADD COLUMN quality_score REAL;
ALTER TABLE bake_artifact_audits ADD COLUMN decision_reason_code TEXT;
ALTER TABLE bake_artifact_audits ADD COLUMN decision_reason_summary TEXT;
ALTER TABLE bake_artifact_audits ADD COLUMN decision_rule_version TEXT;
ALTER TABLE bake_artifact_audits ADD COLUMN shadow_payload_json TEXT;
