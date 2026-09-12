-- 第二期知识门禁：复用半径维度化 + 跨时间线语义身份去重。
-- 存量行三列均为 NULL：dedup_key IS NULL 不参与近重复匹配，满足「存量不重算、不降级、不隐藏」。
ALTER TABLE bake_knowledge ADD COLUMN dedup_key TEXT;
ALTER TABLE bake_knowledge ADD COLUMN quality_score REAL;
ALTER TABLE bake_knowledge ADD COLUMN gate_rule_version TEXT;

-- 近重复查询固定按 (dedup_key, created_at_ms DESC) 取窗口内最新一条。
CREATE INDEX IF NOT EXISTS idx_bake_knowledge_dedup ON bake_knowledge(dedup_key, created_at_ms DESC);
