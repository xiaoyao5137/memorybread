-- 定时任务接入创作执行链路：
-- 1. scheduled_tasks.executor_kind 选择执行智能体（consult 咨询智能体 / creation 创作智能体）
-- 2. task_executions.creation_history_id 关联本次执行产生的创作记录，供任务页跳转执行过程
-- 3. creation_history.source_kind / source_ref_id 标记记录来源，任务执行流水在创作记录中可见
ALTER TABLE scheduled_tasks
    ADD COLUMN executor_kind TEXT NOT NULL DEFAULT 'consult';

ALTER TABLE task_executions
    ADD COLUMN creation_history_id INTEGER;

ALTER TABLE creation_history
    ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'creation';

ALTER TABLE creation_history
    ADD COLUMN source_ref_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_task_executions_creation_history
    ON task_executions(creation_history_id);

CREATE INDEX IF NOT EXISTS idx_creation_history_source
    ON creation_history(source_kind, source_ref_id);
