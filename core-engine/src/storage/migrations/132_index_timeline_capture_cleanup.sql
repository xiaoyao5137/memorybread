-- Capture 保留期清理会按 capture_id 判断是否已有 timeline 提炼结果。
-- 缺少该索引时，每个旧 capture 都会重复全表扫描 timelines。
CREATE INDEX IF NOT EXISTS idx_timelines_capture_id
ON timelines(capture_id);
