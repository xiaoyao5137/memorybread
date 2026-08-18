-- 096: 使用整批 Timeline Facts 重新生成清晰的数据集名称与摘要。
-- canonical key 保持 v16，重物化会原位更新 data_sources 和 snapshots，不创建重复数据项。
DELETE FROM data_timeline_materialization_state;
