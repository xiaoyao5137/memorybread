-- Persist the work context already produced by timeline extraction and expose the
-- selected SOP evidence channel in the bake audit funnel.
ALTER TABLE timelines ADD COLUMN work_item TEXT;
ALTER TABLE timelines ADD COLUMN work_status TEXT;
ALTER TABLE timelines ADD COLUMN work_progress TEXT;

CREATE INDEX IF NOT EXISTS idx_timelines_work_item ON timelines(work_item);
CREATE INDEX IF NOT EXISTS idx_timelines_work_status ON timelines(work_status);

ALTER TABLE bake_candidate_audits ADD COLUMN sop_evidence_mode TEXT;
