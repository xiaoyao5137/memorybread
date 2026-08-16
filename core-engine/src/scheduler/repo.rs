//! 定时任务数据库操作

use rusqlite::params;

use super::models::{
    normalize_executor_kind, NewScheduledTask, ScheduledTask, TaskExecution,
    TaskNotificationDelivery, UpdateScheduledTask,
};
use crate::storage::{StorageError, StorageManager};

pub struct TaskRepo;

impl TaskRepo {
    /// 创建任务，返回新 id
    pub fn create(
        storage: &StorageManager,
        task: &NewScheduledTask,
        now_ms: i64,
    ) -> Result<i64, StorageError> {
        storage.with_conn(|conn| {
            let notification_channel_ids = serde_json::to_string(&task.notification_channel_ids)
                .unwrap_or_else(|_| "[]".into());
            let executor_kind = normalize_executor_kind(&task.executor_kind);
            conn.execute(
                "INSERT INTO scheduled_tasks
                 (name, user_instruction, cron_expression, template_id, notification_channel_ids,
                  executor_kind, enabled, run_count, created_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, 0, ?7, ?7)",
                params![
                    task.name,
                    task.user_instruction,
                    task.cron_expression,
                    task.template_id,
                    notification_channel_ids,
                    executor_kind,
                    now_ms
                ],
            )?;
            Ok(conn.last_insert_rowid())
        })
    }

    /// 查询所有启用的任务
    pub fn list_enabled(storage: &StorageManager) -> Result<Vec<ScheduledTask>, StorageError> {
        storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, name, user_instruction, cron_expression, enabled, template_id,
                        is_builtin, notification_channel_ids, run_count, last_run_at,
                        last_run_status, next_run_at, created_at, updated_at, executor_kind
                 FROM scheduled_tasks WHERE enabled = 1 ORDER BY id",
            )?;
            let rows = stmt.query_map([], |row| Self::row_to_task(row))?;
            Ok(rows.filter_map(|r| r.ok()).collect())
        })
    }

    /// 查询所有任务（含禁用）
    pub fn list_all(storage: &StorageManager) -> Result<Vec<ScheduledTask>, StorageError> {
        storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, name, user_instruction, cron_expression, enabled, template_id,
                        is_builtin, notification_channel_ids, run_count, last_run_at,
                        last_run_status, next_run_at, created_at, updated_at, executor_kind
                 FROM scheduled_tasks ORDER BY id",
            )?;
            let rows = stmt.query_map([], |row| Self::row_to_task(row))?;
            Ok(rows.filter_map(|r| r.ok()).collect())
        })
    }

    /// 按 id 查询单个任务
    pub fn get(storage: &StorageManager, id: i64) -> Result<Option<ScheduledTask>, StorageError> {
        storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, name, user_instruction, cron_expression, enabled, template_id,
                        is_builtin, notification_channel_ids, run_count, last_run_at,
                        last_run_status, next_run_at, created_at, updated_at, executor_kind
                 FROM scheduled_tasks WHERE id = ?1",
            )?;
            let mut rows = stmt.query_map(params![id], |row| Self::row_to_task(row))?;
            Ok(rows.next().and_then(|r| r.ok()))
        })
    }

    /// 更新任务字段
    pub fn update(
        storage: &StorageManager,
        id: i64,
        patch: &UpdateScheduledTask,
        now_ms: i64,
    ) -> Result<bool, StorageError> {
        storage.with_conn(|conn| {
            let notification_channel_ids = patch
                .notification_channel_ids
                .as_ref()
                .map(|ids| serde_json::to_string(ids).unwrap_or_else(|_| "[]".into()));
            let executor_kind = patch
                .executor_kind
                .as_deref()
                .map(normalize_executor_kind);
            let affected = conn.execute(
                "UPDATE scheduled_tasks SET
                   name             = COALESCE(?1, name),
                   user_instruction = COALESCE(?2, user_instruction),
                   cron_expression  = COALESCE(?3, cron_expression),
                   enabled          = COALESCE(?4, enabled),
                   notification_channel_ids = COALESCE(?5, notification_channel_ids),
                   updated_at       = ?6,
                   executor_kind    = COALESCE(?7, executor_kind)
                 WHERE id = ?8",
                params![
                    patch.name,
                    patch.user_instruction,
                    patch.cron_expression,
                    patch.enabled.map(|b| b as i64),
                    notification_channel_ids,
                    now_ms,
                    executor_kind,
                    id,
                ],
            )?;
            Ok(affected > 0)
        })
    }

    /// 删除任务（级联删除执行历史）
    pub fn delete(storage: &StorageManager, id: i64) -> Result<bool, StorageError> {
        storage.with_conn(|conn| {
            let affected =
                conn.execute("DELETE FROM scheduled_tasks WHERE id = ?1", params![id])?;
            Ok(affected > 0)
        })
    }

    /// 更新 next_run_at
    pub fn set_next_run(
        storage: &StorageManager,
        id: i64,
        next_ms: i64,
    ) -> Result<(), StorageError> {
        storage.with_conn(|conn| {
            conn.execute(
                "UPDATE scheduled_tasks SET next_run_at = ?1 WHERE id = ?2",
                params![next_ms, id],
            )?;
            Ok(())
        })
    }

    /// 将历史五段 cron 规范化，并把下一次执行时间推进到未来。
    pub fn repair_schedule(
        storage: &StorageManager,
        id: i64,
        cron_expression: &str,
        next_ms: i64,
        now_ms: i64,
    ) -> Result<(), StorageError> {
        storage.with_conn(|conn| {
            conn.execute(
                "UPDATE scheduled_tasks
                 SET cron_expression = ?1, next_run_at = ?2, updated_at = ?3
                 WHERE id = ?4",
                params![cron_expression, next_ms, now_ms, id],
            )?;
            Ok(())
        })
    }

    /// 无效 cron 必须停止调度，避免 next_run_at 卡在过去并持续重试。
    pub fn disable_invalid_schedule(
        storage: &StorageManager,
        id: i64,
        now_ms: i64,
    ) -> Result<(), StorageError> {
        storage.with_conn(|conn| {
            conn.execute(
                "UPDATE scheduled_tasks
                 SET enabled = 0,
                     last_run_status = 'invalid_schedule',
                     next_run_at = NULL,
                     updated_at = ?1
                 WHERE id = ?2",
                params![now_ms, id],
            )?;
            Ok(())
        })
    }

    /// 查询任务的执行历史
    pub fn list_executions(
        storage: &StorageManager,
        task_id: i64,
        limit: i64,
    ) -> Result<Vec<TaskExecution>, StorageError> {
        storage.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, task_id, started_at, completed_at, status,
                        knowledge_count, token_used, result_text, error_message, latency_ms,
                        creation_history_id
                 FROM task_executions WHERE task_id = ?1
                 ORDER BY started_at DESC LIMIT ?2",
            )?;
            let rows = stmt.query_map(params![task_id, limit], |row| {
                Ok(TaskExecution {
                    id: row.get(0)?,
                    task_id: row.get(1)?,
                    started_at: row.get(2)?,
                    completed_at: row.get(3)?,
                    status: row.get(4)?,
                    knowledge_count: row.get(5)?,
                    token_used: row.get(6)?,
                    result_text: row.get(7)?,
                    error_message: row.get(8)?,
                    latency_ms: row.get(9)?,
                    creation_history_id: row.get(10)?,
                    notification_deliveries: Vec::new(),
                })
            })?;
            let mut executions = rows.filter_map(Result::ok).collect::<Vec<_>>();
            for execution in &mut executions {
                let mut delivery_statement = conn.prepare(
                    "SELECT d.channel_id, c.name, c.channel_type, d.status,
                            d.error_message, d.delivered_at
                     FROM task_notification_deliveries d
                     JOIN notification_channels c ON c.id = d.channel_id
                     WHERE d.execution_id = ?1
                     ORDER BY d.id",
                )?;
                let deliveries = delivery_statement.query_map(params![execution.id], |row| {
                    Ok(TaskNotificationDelivery {
                        channel_id: row.get(0)?,
                        channel_name: row.get(1)?,
                        channel_type: row.get(2)?,
                        status: row.get(3)?,
                        error_message: row.get(4)?,
                        delivered_at: row.get(5)?,
                    })
                })?;
                execution.notification_deliveries = deliveries.filter_map(Result::ok).collect();
            }
            Ok(executions)
        })
    }

    fn row_to_task(row: &rusqlite::Row<'_>) -> rusqlite::Result<ScheduledTask> {
        Ok(ScheduledTask {
            id: row.get(0)?,
            name: row.get(1)?,
            user_instruction: row.get(2)?,
            cron_expression: row.get(3)?,
            enabled: row.get::<_, i64>(4)? != 0,
            template_id: row.get(5)?,
            is_builtin: row.get::<_, i64>(6)? != 0,
            can_delete: row.get::<_, i64>(6)? == 0,
            notification_channel_ids: serde_json::from_str(&row.get::<_, String>(7)?)
                .unwrap_or_default(),
            run_count: row.get(8)?,
            last_run_at: row.get(9)?,
            last_run_status: row.get(10)?,
            next_run_at: row.get(11)?,
            created_at: row.get(12)?,
            updated_at: row.get(13)?,
            executor_kind: row.get::<_, Option<String>>(14)?.unwrap_or_else(|| "consult".into()),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::StorageManager;

    fn new_task(name: &str, executor_kind: &str) -> NewScheduledTask {
        NewScheduledTask {
            name: name.into(),
            user_instruction: "生成行业周报".into(),
            cron_expression: "0 9 * * *".into(),
            template_id: None,
            notification_channel_ids: Vec::new(),
            executor_kind: executor_kind.into(),
        }
    }

    #[test]
    fn create_and_read_executor_kind() {
        let storage = StorageManager::open_in_memory().unwrap();
        let creation_id = TaskRepo::create(&storage, &new_task("创作任务", "creation"), 1).unwrap();
        let consult_id = TaskRepo::create(&storage, &new_task("咨询任务", "consult"), 1).unwrap();
        // 非法取值归一化为默认咨询智能体。
        let fallback_id = TaskRepo::create(&storage, &new_task("未知任务", "unknown"), 1).unwrap();

        let creation = TaskRepo::get(&storage, creation_id).unwrap().unwrap();
        let consult = TaskRepo::get(&storage, consult_id).unwrap().unwrap();
        let fallback = TaskRepo::get(&storage, fallback_id).unwrap().unwrap();
        assert_eq!(creation.executor_kind, "creation");
        assert_eq!(consult.executor_kind, "consult");
        assert_eq!(fallback.executor_kind, "consult");

        let all = TaskRepo::list_all(&storage).unwrap();
        // 内存库会预置内置日记任务，只校验本次创建的三条。
        let created = all
            .iter()
            .filter(|task| ["创作任务", "咨询任务", "未知任务"].contains(&task.name.as_str()))
            .collect::<Vec<_>>();
        assert_eq!(created.len(), 3);
        assert!(created
            .iter()
            .all(|task| ["creation", "consult"].contains(&task.executor_kind.as_str())));
    }

    #[test]
    fn update_executor_kind_without_touching_other_fields() {
        let storage = StorageManager::open_in_memory().unwrap();
        let task_id = TaskRepo::create(&storage, &new_task("任务", "consult"), 1).unwrap();

        let updated = TaskRepo::update(
            &storage,
            task_id,
            &UpdateScheduledTask {
                name: None,
                user_instruction: None,
                cron_expression: None,
                enabled: None,
                notification_channel_ids: None,
                executor_kind: Some("creation".into()),
            },
            2,
        )
        .unwrap();
        assert!(updated);
        let task = TaskRepo::get(&storage, task_id).unwrap().unwrap();
        assert_eq!(task.executor_kind, "creation");
        assert_eq!(task.name, "任务");

        // patch 不携带 executor_kind 时保持原值。
        TaskRepo::update(
            &storage,
            task_id,
            &UpdateScheduledTask {
                name: Some("改名".into()),
                user_instruction: None,
                cron_expression: None,
                enabled: None,
                notification_channel_ids: None,
                executor_kind: None,
            },
            3,
        )
        .unwrap();
        let task = TaskRepo::get(&storage, task_id).unwrap().unwrap();
        assert_eq!(task.executor_kind, "creation");
        assert_eq!(task.name, "改名");
    }

    #[test]
    fn list_executions_returns_creation_history_id() {
        let storage = StorageManager::open_in_memory().unwrap();
        let task_id = TaskRepo::create(&storage, &new_task("创作任务", "creation"), 1).unwrap();
        storage
            .with_conn(|conn| {
                conn.execute(
                    "INSERT INTO task_executions
                     (task_id, started_at, completed_at, status, result_text, creation_history_id)
                     VALUES (?1, 10, 20, 'success', '文档正文', 88)",
                    params![task_id],
                )?;
                conn.execute(
                    "INSERT INTO task_executions
                     (task_id, started_at, completed_at, status, result_text)
                     VALUES (?1, 5, 8, 'success', '旧结果')",
                    params![task_id],
                )?;
                Ok(())
            })
            .unwrap();

        let executions = TaskRepo::list_executions(&storage, task_id, 10).unwrap();
        assert_eq!(executions.len(), 2);
        // 按 started_at 倒序，最新一条携带创作记录关联。
        assert_eq!(executions[0].creation_history_id, Some(88));
        assert_eq!(executions[1].creation_history_id, None);
    }
}
