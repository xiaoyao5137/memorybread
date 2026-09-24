-- Capture 保留期清理会保留 timeline，但历史实现临时关闭外键删除 capture，
-- 因此需要再次正规化部署窗口内新产生的直接 capture 引用。
UPDATE data_source_links
SET capture_id = NULL
WHERE capture_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM captures WHERE captures.id = data_source_links.capture_id
  );
