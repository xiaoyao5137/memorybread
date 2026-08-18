-- 093: 将单指标 work_memory 收敛为 timeline 数据集。
-- 仅清空内部物化状态，后台会分批重建聚合数据集；旧数据在新数据集成功落库后再安全停用。
DELETE FROM data_timeline_materialization_state;
