ALTER TABLE timeline_data_facts ADD COLUMN semantic_relation TEXT NOT NULL DEFAULT '';
ALTER TABLE timeline_data_facts ADD COLUMN future_question TEXT NOT NULL DEFAULT '';
ALTER TABLE timeline_data_facts ADD COLUMN decision_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE timeline_data_facts ADD COLUMN decision_state TEXT NOT NULL DEFAULT 'published';
ALTER TABLE timeline_data_facts ADD COLUMN decision_rule_version TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_timeline_data_facts_decision
ON timeline_data_facts(decision_state, timeline_id, id);
