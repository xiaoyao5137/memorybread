//! 定时任务调度运行器
//!
//! 每 30 秒轮询一次数据库，找出到期任务，通过 HTTP 调用 Python TaskExecutor。

use std::collections::HashSet;
use std::sync::Arc;

use chrono::Utc;
use reqwest::StatusCode;
use tokio::sync::{Mutex, Semaphore};
use tokio::time::{interval, Duration};
use tracing::{debug, error, info, warn};

use super::{
    cron_expression::{next_run_at_ms, normalize_cron_expression},
    repo::TaskRepo,
};
use crate::storage::StorageManager;

const POLL_INTERVAL_SECS: u64 = 30;
const PYTHON_EXECUTOR_URL: &str = "http://127.0.0.1:7071/tasks/execute";
/// 允许同时触发的最大任务数，防止长时间离线后恢复时大量任务并发打垮 Python executor
const MAX_CONCURRENT_TRIGGERS: usize = 5;
const BUSY_RETRY_BACKOFF_MS: i64 = 5 * 60 * 1000;

pub struct Scheduler {
    storage: StorageManager,
    client: reqwest::Client,
    trigger_limit: Arc<Semaphore>,
    in_flight: Arc<Mutex<HashSet<i64>>>,
}

impl Scheduler {
    pub fn new(storage: StorageManager) -> Self {
        Self {
            storage,
            client: reqwest::Client::new(),
            trigger_limit: Arc::new(Semaphore::new(MAX_CONCURRENT_TRIGGERS)),
            in_flight: Arc::new(Mutex::new(HashSet::new())),
        }
    }

    /// 启动调度循环（在独立 tokio task 中运行）
    pub async fn run(self: Arc<Self>) {
        info!("定时任务调度器启动，轮询间隔 {}s", POLL_INTERVAL_SECS);
        let mut ticker = interval(Duration::from_secs(POLL_INTERVAL_SECS));

        loop {
            ticker.tick().await;
            if let Err(e) = self.tick().await {
                error!("调度器轮询异常: {e}");
            }
        }
    }

    async fn tick(&self) -> anyhow::Result<()> {
        let now_ms = Utc::now().timestamp_millis();
        let tasks = TaskRepo::list_enabled(&self.storage)?;

        for task in tasks {
            let normalized_cron = match normalize_cron_expression(&task.cron_expression) {
                Ok(expression) => expression,
                Err(error) => {
                    error!(task_id = task.id, %error, "任务 cron 无效，已自动停用");
                    TaskRepo::disable_invalid_schedule(&self.storage, task.id, now_ms)?;
                    continue;
                }
            };

            let next_run = if normalized_cron != task.cron_expression {
                let next = Self::calc_next_run_static(&normalized_cron)?;
                TaskRepo::repair_schedule(&self.storage, task.id, &normalized_cron, next, now_ms)?;
                warn!(
                    task_id = task.id,
                    cron = %normalized_cron,
                    next_run_at = next,
                    "历史五段 cron 已规范化并推进执行时间"
                );
                next
            } else {
                match task.next_run_at {
                    Some(next) => next,
                    None => {
                        let next = self.calc_next_run(&normalized_cron)?;
                        TaskRepo::set_next_run(&self.storage, task.id, next)?;
                        next
                    }
                }
            };

            if now_ms >= next_run {
                let inserted = self.in_flight.lock().await.insert(task.id);
                if !inserted {
                    debug!(task_id = task.id, "任务仍在执行，跳过重复触发");
                    continue;
                }

                info!("触发任务: id={}, name={}", task.id, task.name);

                let sem = self.trigger_limit.clone();
                let in_flight = self.in_flight.clone();
                let client = self.client.clone();
                let task_id = task.id;
                let cron_expr = normalized_cron;
                let storage = self.storage.clone();

                tokio::spawn(async move {
                    let permit = sem.acquire_owned().await;
                    if permit.is_err() {
                        in_flight.lock().await.remove(&task_id);
                        return;
                    }
                    let _permit = permit.unwrap();

                    match Self::trigger_task(&client, task_id).await {
                        Ok(TriggerOutcome::Completed) => {
                            Self::advance_to_next_schedule(&storage, task_id, &cron_expr);
                        }
                        Ok(TriggerOutcome::RetryWhenIdle) => {
                            let retry_at = Utc::now().timestamp_millis() + BUSY_RETRY_BACKOFF_MS;
                            if let Err(error) = TaskRepo::set_next_run(&storage, task_id, retry_at)
                            {
                                error!(task_id, %error, "写入任务繁忙退避时间失败");
                            } else {
                                warn!(task_id, retry_at, "sidecar 繁忙，任务将在 5 分钟后重试");
                            }
                        }
                        Err(e) => {
                            error!("任务触发失败: id={task_id}, error={e}");
                            Self::advance_to_next_schedule(&storage, task_id, &cron_expr);
                        }
                    }

                    in_flight.lock().await.remove(&task_id);
                });
            }
        }

        Ok(())
    }

    async fn trigger_task(
        client: &reqwest::Client,
        task_id: i64,
    ) -> anyhow::Result<TriggerOutcome> {
        let resp = client
            .post(PYTHON_EXECUTOR_URL)
            .json(&serde_json::json!({ "task_id": task_id }))
            .timeout(std::time::Duration::from_secs(1800)) // 创作智能体单轮耗时更长，最长等待30分钟
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            if Self::should_retry_when_sidecar_busy(status) {
                return Ok(TriggerOutcome::RetryWhenIdle);
            }
            anyhow::bail!("Python executor 返回错误: {body}");
        }
        Ok(TriggerOutcome::Completed)
    }

    fn should_retry_when_sidecar_busy(status: StatusCode) -> bool {
        matches!(
            status,
            StatusCode::TOO_MANY_REQUESTS | StatusCode::SERVICE_UNAVAILABLE
        )
    }

    fn calc_next_run(&self, cron_expr: &str) -> anyhow::Result<i64> {
        Self::calc_next_run_static(cron_expr)
    }

    fn calc_next_run_static(cron_expr: &str) -> anyhow::Result<i64> {
        next_run_at_ms(cron_expr)
            .map(|(_, next)| next)
            .map_err(anyhow::Error::msg)
    }

    fn advance_to_next_schedule(storage: &StorageManager, task_id: i64, cron_expr: &str) {
        match Self::calc_next_run_static(cron_expr) {
            Ok(next) => match TaskRepo::set_next_run(storage, task_id, next) {
                Ok(()) => info!("任务 {task_id} 下次执行: {next}"),
                Err(error) => error!(task_id, %error, "写入任务下次执行时间失败"),
            },
            Err(error) => error!(task_id, %error, "计算任务下次执行时间失败"),
        }
    }
}

enum TriggerOutcome {
    Completed,
    RetryWhenIdle,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn busy_status_keeps_task_due_for_idle_retry() {
        assert!(Scheduler::should_retry_when_sidecar_busy(
            StatusCode::SERVICE_UNAVAILABLE
        ));
        assert!(Scheduler::should_retry_when_sidecar_busy(
            StatusCode::TOO_MANY_REQUESTS
        ));
        assert!(!Scheduler::should_retry_when_sidecar_busy(
            StatusCode::INTERNAL_SERVER_ERROR
        ));
    }

    #[test]
    fn legacy_five_field_cron_can_calculate_next_run() {
        let next = Scheduler::calc_next_run_static("0 9 * * *").unwrap();
        assert!(next > Utc::now().timestamp_millis());
    }
}
