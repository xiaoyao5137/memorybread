-- Record the bake queue actionable count observed at trigger time, so monitoring
-- can compare the queue-status snapshot against what the run actually selected.
ALTER TABLE bake_runs ADD COLUMN trigger_actionable_count INTEGER;
