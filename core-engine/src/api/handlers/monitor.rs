//! 监控 API 处理器
//!
//! GET /api/monitor/overview?range_ms=21600000
//! 聚合返回：token 用量、采集流水、问答记录、定时任务执行

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use axum::{
    extract::{Query, State},
    response::IntoResponse,
    Json,
};
use chrono::{Local, TimeZone, Utc};
use serde::{Deserialize, Serialize};

use crate::{
    api::{error::ApiError, state::AppState},
    capture::engine::{ocr_backfill_metrics_snapshot, OcrBackfillMetricsSnapshot},
    services::bake_service::{BAKE_GENERATION_VERSION, MAX_BAKE_RETRY_FAILURES},
    storage::{
        models_bake::BakeQueueStatusRecord,
        repo::bake_run::{
            load_bake_production_events, refresh_doc_member_temp_tables, PRODUCED_DOC_TIMELINES_CTE,
        },
    },
};

const SELF_GENERATED_APP_KEYWORDS: [&str; 2] = ["memory-bread", "记忆面包"];
const SELF_GENERATED_WINDOW_KEYWORDS: [&str; 6] = [
    "memory-bread",
    "memorybread preview",
    "记忆面包",
    "KnowledgePanel",
    "MonitorPanel",
    "RagPanel",
];
const FALLBACK_NOISE_OVERVIEW_PREFIX: &str = "低价值工作片段（";
const SYSTEM_SCOPE: &str = "system_global";
const SUITE_SCOPE: &str = "app_suite_total";
const MODEL_SCOPE: &str = "model_process_total";
const MODEL_SERIES_SCOPE: &str = "model_runtime_series";
const BAKE_WATERMARK_STALL_MS: i64 = 15 * 60 * 1000;

fn build_not_like_clause(column: &str, keywords: &[&str]) -> String {
    keywords
        .iter()
        .map(|keyword| format!("LOWER({column}) NOT LIKE '%{}%'", keyword.to_lowercase()))
        .collect::<Vec<_>>()
        .join(" AND ")
}

fn is_bake_pipeline_stalled(
    capture_enabled: bool,
    now_ms: i64,
    pending_bake_count: i64,
    oldest_pending_bake_at_ms: Option<i64>,
    bake_watermark_updated_at_ms: Option<i64>,
) -> bool {
    if !capture_enabled {
        return false;
    }
    let backlog_old_enough = oldest_pending_bake_at_ms
        .map(|oldest| now_ms.saturating_sub(oldest) >= BAKE_WATERMARK_STALL_MS)
        .unwrap_or(false);
    let watermark_stale = bake_watermark_updated_at_ms
        .map(|updated| now_ms.saturating_sub(updated) >= BAKE_WATERMARK_STALL_MS)
        .unwrap_or(true);
    pending_bake_count > 0 && backlog_old_enough && watermark_stale
}

fn load_pipeline_backlog_metrics(
    conn: &rusqlite::Connection,
    now_ms: i64,
    capture_enabled: bool,
    queue_status: &BakeQueueStatusRecord,
) -> Result<PipelineBacklogMetrics, rusqlite::Error> {
    let app_not_like = build_not_like_clause("c.app_name", &SELF_GENERATED_APP_KEYWORDS);
    let win_not_like = build_not_like_clause("c.win_title", &SELF_GENERATED_WINDOW_KEYWORDS);
    let pending_capture_sql = format!(
        "SELECT COUNT(*), MIN(c.ts)
         FROM captures c
         WHERE (
               COALESCE(c.ocr_text, '') != ''
            OR COALESCE(c.ax_text, '') != ''
            OR COALESCE(c.input_text, '') != ''
            OR COALESCE(c.audio_text, '') != ''
         )
           AND c.timeline_id IS NULL
           AND c.is_sensitive = 0
           AND ({app_not_like})
           AND ({win_not_like})"
    );
    let (pending_extraction_count, oldest_pending_extraction_at_ms) =
        conn.query_row(&pending_capture_sql, [], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, Option<i64>>(1)?))
        })?;

    // 消化速率：取最近 6 小时已完成 run 的候选处理量折算每小时吞吐，
    // 给监控页展示积压预计清空时间；速率过低时 ETA 不可信，返回 None。
    const BAKE_DRAIN_WINDOW_MS: i64 = 6 * 60 * 60 * 1000;
    let bake_drain_window_hours = (BAKE_DRAIN_WINDOW_MS / (60 * 60 * 1000)) as f64;
    let bake_drain_processed_in_window: i64 = conn
        .query_row(
            "SELECT COALESCE(SUM(processed_episode_count), 0)
             FROM bake_runs
             WHERE completed_at IS NOT NULL AND completed_at >= ?1",
            rusqlite::params![now_ms - BAKE_DRAIN_WINDOW_MS],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let bake_drain_rate_per_hour = bake_drain_processed_in_window as f64 / bake_drain_window_hours;
    let running_bake_count = conn
        .query_row(
            "SELECT COUNT(*) FROM bake_runs
             WHERE status = 'running' AND started_at >= ?1",
            rusqlite::params![now_ms - DAG_RUNNING_BAKE_STALE_MS],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let stale_bake_run_count = conn
        .query_row(
            "SELECT COUNT(*) FROM bake_runs
             WHERE status = 'running' AND started_at < ?1",
            rusqlite::params![now_ms - DAG_RUNNING_BAKE_STALE_MS],
            |row| row.get(0),
        )
        .unwrap_or(0);
    let latest_bake_status = conn
        .query_row(
            "SELECT status FROM bake_runs ORDER BY started_at DESC, id DESC LIMIT 1",
            [],
            |row| row.get::<_, String>(0),
        )
        .ok();
    let (recent_bake_run_count, recent_bake_failed_count, recent_bake_deferred_count) = conn
        .query_row(
            "SELECT COUNT(*),
                    COALESCE(SUM(status = 'failed'), 0),
                    COALESCE(SUM(status = 'deferred'), 0)
             FROM (
                 SELECT status FROM bake_runs
                 ORDER BY started_at DESC, id DESC
                 LIMIT 5
             )",
            [],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            },
        )
        .unwrap_or((0, 0, 0));
    // 调度、预检、监控统一使用 Core 队列快照；上面的历史 SQL 暂保留用于兼容
    // 旧库诊断字段，但不再决定对外的库存和重试数量。
    let pending_bake_count = queue_status.pending_count;
    let oldest_pending_bake_at_ms = match (
        queue_status.oldest_fresh_at_ms,
        queue_status.oldest_retry_at_ms,
    ) {
        (Some(fresh), Some(retry)) => Some(fresh.min(retry)),
        (Some(fresh), None) => Some(fresh),
        (None, Some(retry)) => Some(retry),
        (None, None) => None,
    };
    let bake_retry_pending_count = queue_status
        .retry_ready_count
        .saturating_add(queue_status.retry_delayed_count);
    let bake_retry_exhausted_count = queue_status.dead_letter_count;
    let bake_timeout_failure_count = queue_status.retry_timeout_count;
    let bake_truncated_failure_count = queue_status.retry_output_count;
    let bake_other_failure_count = queue_status.retry_other_count;
    let bake_watermark_updated_at_ms = queue_status.watermark_updated_at_ms;
    let bake_eta_ms = if pending_bake_count > 0 && bake_drain_rate_per_hour >= 0.5 {
        Some(((pending_bake_count as f64 / bake_drain_rate_per_hour) * 3_600_000.0) as i64)
    } else {
        None
    };
    let bake_stalled = is_bake_pipeline_stalled(
        capture_enabled,
        now_ms,
        pending_bake_count,
        oldest_pending_bake_at_ms,
        bake_watermark_updated_at_ms,
    );
    let bake_scheduler_mismatch =
        queue_status.actionable_count > 0 && queue_status.recent_no_progress_count >= 3;
    let recent_bake_runs_unhealthy = recent_bake_run_count >= 3
        && (recent_bake_failed_count + recent_bake_deferred_count >= 3
            || queue_status.recent_no_progress_count >= 3);

    Ok(PipelineBacklogMetrics {
        pending_extraction_count,
        oldest_pending_extraction_at_ms,
        pending_bake_count,
        oldest_pending_bake_at_ms,
        bake_drain_rate_per_hour,
        bake_eta_ms,
        bake_retry_pending_count,
        bake_fresh_pending_count: queue_status.fresh_count,
        bake_retry_ready_count: queue_status.retry_ready_count,
        bake_retry_delayed_count: queue_status.retry_delayed_count,
        bake_actionable_count: queue_status.actionable_count,
        bake_retry_exhausted_count,
        bake_timeout_failure_count,
        bake_truncated_failure_count,
        bake_other_failure_count,
        bake_upstream_failure_count: queue_status.retry_upstream_count,
        bake_recent_no_progress_count: queue_status.recent_no_progress_count,
        bake_next_retry_at_ms: queue_status.next_retry_at_ms,
        bake_recommended_retry_after_ms: queue_status.recommended_retry_after_ms,
        bake_scheduler_mismatch,
        running_bake_count,
        stale_bake_run_count,
        bake_watermark_updated_at_ms,
        latest_bake_status,
        recent_bake_run_count,
        recent_bake_failed_count,
        recent_bake_deferred_count,
        recent_bake_runs_unhealthy,
        bake_stalled,
    })
}

async fn read_inference_queue_monitor(sidecar_url: &str) -> InferenceQueueMonitor {
    let url = format!(
        "{}/api/inference/queue-status",
        sidecar_url.trim_end_matches('/')
    );
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_millis(800))
        .build()
    {
        Ok(client) => client,
        Err(_) => return InferenceQueueMonitor::default(),
    };
    let response = match client.get(url).send().await {
        Ok(response) if response.status().is_success() => response,
        _ => return InferenceQueueMonitor::default(),
    };
    let body = match response.json::<UpstreamInferenceQueueResponse>().await {
        Ok(body) if body.status == "ok" => body,
        _ => return InferenceQueueMonitor::default(),
    };
    let Some(stats) = body.stats else {
        return InferenceQueueMonitor::default();
    };
    let queued_p0 = stats.queue_lengths.get("P0").copied().unwrap_or(0);
    let queued_p1 = stats.queue_lengths.get("P1").copied().unwrap_or(0);
    let queued_p2 = stats.queue_lengths.get("P2").copied().unwrap_or(0);
    let running_p2 = stats.running_by_lane.get("p2_bake").copied().unwrap_or(0);
    InferenceQueueMonitor {
        available: true,
        idle: body.idle,
        queued_total: queued_p0 + queued_p1 + queued_p2,
        queued_p0,
        queued_p1,
        queued_p2,
        running_total: stats.running_total,
        running_p2,
        oldest_wait_ms: stats.oldest_wait_ms,
        max_concurrency: stats.max_concurrency,
        on_external_power: stats.on_external_power,
    }
}

// ── 请求参数 ─────────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct MonitorQuery {
    /// 兼容旧参数：1h | 6h | 24h | 1d | 7d | 30d
    #[serde(default = "default_range")]
    pub range: String,
    /// 动态时间宽幅（毫秒），优先于 range。默认最近 6 小时。
    pub range_ms: Option<i64>,
}

fn default_range() -> String {
    "6h".to_string()
}

fn range_to_ms(range: &str) -> i64 {
    match range {
        "1h" => 3600 * 1000,
        "6h" => 6 * 3600 * 1000,
        "24h" | "1d" => 24 * 3600 * 1000,
        "30d" => 30 * 24 * 3600 * 1000,
        _ => 7 * 24 * 3600 * 1000,
    }
}

fn query_range_ms(params: &MonitorQuery) -> i64 {
    const MIN_RANGE_MS: i64 = 60 * 1000;
    const MAX_RANGE_MS: i64 = 90 * 24 * 3600 * 1000;
    params
        .range_ms
        .unwrap_or_else(|| range_to_ms(&params.range))
        .clamp(MIN_RANGE_MS, MAX_RANGE_MS)
}

fn nice_bucket_ms(range_ms: i64) -> i64 {
    const TARGET_POINTS: i64 = 80;
    const BUCKETS: [i64; 8] = [
        60 * 1000,
        5 * 60 * 1000,
        15 * 60 * 1000,
        60 * 60 * 1000,
        3 * 60 * 60 * 1000,
        6 * 60 * 60 * 1000,
        12 * 60 * 60 * 1000,
        24 * 60 * 60 * 1000,
    ];
    let wanted = (range_ms / TARGET_POINTS).max(BUCKETS[0]);
    BUCKETS
        .iter()
        .copied()
        .find(|bucket| *bucket >= wanted)
        .unwrap_or(*BUCKETS.last().unwrap())
}

fn is_sub_day_range(range_ms: i64) -> bool {
    range_ms <= 24 * 3600 * 1000
}

fn trend_bucket_ms(range_ms: i64) -> i64 {
    nice_bucket_ms(range_ms)
}

fn knowledge_bucket_ms(range_ms: i64) -> i64 {
    nice_bucket_ms(range_ms)
}

fn bucket_label(range_ms: i64, bucket_start_ms: i64) -> String {
    let local_dt = chrono::DateTime::<Utc>::from_timestamp_millis(bucket_start_ms)
        .map(|dt| dt.with_timezone(&Local))
        .unwrap_or_else(Local::now);
    if is_sub_day_range(range_ms) {
        local_dt.format("%H:%M").to_string()
    } else {
        local_dt.format("%m-%d").to_string()
    }
}

fn local_day_start_ms(now_ms: i64) -> i64 {
    let now_local = chrono::DateTime::<Utc>::from_timestamp_millis(now_ms)
        .map(|dt| dt.with_timezone(&Local))
        .unwrap_or_else(Local::now);
    let naive_start = now_local.date_naive().and_hms_opt(0, 0, 0).unwrap();
    Local
        .from_local_datetime(&naive_start)
        .earliest()
        .or_else(|| Local.from_local_datetime(&naive_start).latest())
        .unwrap_or(now_local)
        .with_timezone(&Utc)
        .timestamp_millis()
}

fn system_bucket_ms(range: &str) -> i64 {
    match range {
        "1h" => 60 * 1000,
        "6h" => 3 * 60 * 1000,
        "24h" => 60 * 1000,
        "1d" => 60 * 1000,
        _ => 60 * 1000,
    }
}

fn public_llm_model_name(model: &str) -> String {
    let normalized = model.trim().to_ascii_lowercase();
    if normalized == "unavailable" {
        return normalized;
    }
    if normalized.contains("plus") || normalized.contains("opus") {
        return "MBCD Plus v1.0".to_string();
    }
    "MBEM v1.0".to_string()
}

fn public_model_event_name(model_type: &str, model_name: &str) -> String {
    match model_type {
        "llm" => public_llm_model_name(model_name),
        "embedding" => "MBEMB V1.0".to_string(),
        _ => model_name.to_string(),
    }
}

// ── 响应结构 ─────────────────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct MonitorOverview {
    pub db_size_bytes: i64,
    pub capture_total_count: i64,
    pub service_health: ServiceHealth,
    pub token_usage: TokenUsage,
    pub ocr_backfill: OcrBackfillMetricsSnapshot,
    pub capture_flow: CaptureFlow,
    pub knowledge_flow: KnowledgeFlow,
    pub rag_sessions: RagSessionStats,
    pub task_executions: TaskExecutionStats,
}

#[derive(Debug, Serialize, Clone)]
pub struct ServiceHealth {
    pub status: String,
    pub mode: String,
    pub full_dispatch_ready: bool,
    pub background_processor_running: bool,
    pub critical_checks_passed: bool,
    pub embedding_ok: bool,
    pub issues: Vec<String>,
    pub updated_at_ms: Option<i64>,
}

impl ServiceHealth {
    fn unknown(reason: impl Into<String>) -> Self {
        Self {
            status: "down".to_string(),
            mode: "unknown".to_string(),
            full_dispatch_ready: false,
            background_processor_running: false,
            critical_checks_passed: false,
            embedding_ok: false,
            issues: vec![reason.into()],
            updated_at_ms: None,
        }
    }
}

fn apply_bake_pipeline_health(health: &mut ServiceHealth, backlog: &PipelineBacklogMetrics) {
    // Sidecar 队列与 SQLite 是先后采样的，P2 推理也可能在请求方收尾后短暂继续。
    // 单次快照不一致不是故障；这里只使用持续积压和连续异常批次等稳定信号。
    let mut degraded = false;
    if backlog.bake_stalled {
        health.issues.push(format!(
            "烘焙流水线有 {} 条积压且水位超过 15 分钟未推进",
            backlog.pending_bake_count,
        ));
        degraded = true;
    }
    if backlog.recent_bake_runs_unhealthy && backlog.bake_stalled {
        let recent_problem_count =
            backlog.recent_bake_failed_count + backlog.recent_bake_deferred_count;
        health.issues.push(format!(
            "最近 {} 个烘焙批次中有 {} 个失败或延后",
            backlog.recent_bake_run_count, recent_problem_count,
        ));
        degraded = true;
    }
    if degraded && health.status == "ok" {
        health.status = "degraded".to_string();
    }
}

#[derive(Debug, Serialize)]
pub struct TokenUsage {
    pub total_period: i64,
    pub total_today: i64,
    pub by_model: Vec<ModelUsage>,
    pub by_caller: Vec<CallerUsage>,
    pub trend: Vec<DayTrend>,
    pub trend_by_model: Vec<ModelTrend>,
    pub bake_distribution: BakeTokenDistribution,
}

#[derive(Debug, Serialize)]
pub struct BakeTokenDistribution {
    pub sample_count: i64,
    pub truncated_count: i64,
    pub input_over_20k_count: i64,
    pub input: TokenMetricDistribution,
    pub output: TokenMetricDistribution,
}

#[derive(Debug, Serialize)]
pub struct TokenMetricDistribution {
    pub p50: i64,
    pub p90: i64,
    pub p95: i64,
    pub p99: i64,
    pub max: i64,
    pub buckets: Vec<TokenBucket>,
}

#[derive(Debug, Serialize)]
pub struct TokenBucket {
    pub label: String,
    pub count: i64,
}

#[derive(Debug, Serialize)]
pub struct ModelUsage {
    pub model: String,
    pub total: i64,
    pub prompt: i64,
    pub completion: i64,
    pub calls: i64,
}

#[derive(Debug, Serialize)]
pub struct CallerUsage {
    pub caller: String,
    pub total: i64,
    pub calls: i64,
}

#[derive(Debug, Serialize)]
pub struct DayTrend {
    pub ts: i64,
    pub date: String,
    pub tokens: i64,
    pub calls: i64,
}

#[derive(Debug, Serialize)]
pub struct ModelTrend {
    pub model: String,
    pub total: i64,
    pub calls: i64,
    pub trend: Vec<DayTrend>,
}

fn nearest_rank(sorted: &[i64], percentile: f64) -> i64 {
    if sorted.is_empty() {
        return 0;
    }
    let rank = (percentile.clamp(0.0, 1.0) * sorted.len() as f64).ceil() as usize;
    sorted[rank.saturating_sub(1).min(sorted.len() - 1)]
}

fn build_token_buckets(values: &[i64], definitions: &[(&str, Option<i64>)]) -> Vec<TokenBucket> {
    let mut lower = 0_i64;
    definitions
        .iter()
        .map(|(label, upper)| {
            let count = values
                .iter()
                .filter(|value| {
                    **value >= lower && upper.is_none_or(|upper_bound| **value < upper_bound)
                })
                .count() as i64;
            if let Some(upper_bound) = upper {
                lower = *upper_bound;
            }
            TokenBucket {
                label: (*label).to_string(),
                count,
            }
        })
        .collect()
}

fn build_token_metric_distribution(
    mut values: Vec<i64>,
    bucket_definitions: &[(&str, Option<i64>)],
) -> TokenMetricDistribution {
    values.sort_unstable();
    TokenMetricDistribution {
        p50: nearest_rank(&values, 0.50),
        p90: nearest_rank(&values, 0.90),
        p95: nearest_rank(&values, 0.95),
        p99: nearest_rank(&values, 0.99),
        max: values.last().copied().unwrap_or(0),
        buckets: build_token_buckets(&values, bucket_definitions),
    }
}

fn load_bake_token_distribution(
    conn: &rusqlite::Connection,
    from_ms: i64,
) -> rusqlite::Result<BakeTokenDistribution> {
    const INPUT_BUCKETS: &[(&str, Option<i64>)] = &[
        ("<4K", Some(4_000)),
        ("4–8K", Some(8_000)),
        ("8–12K", Some(12_000)),
        ("12–16K", Some(16_000)),
        ("16–20K", Some(20_000)),
        ("20–24K", Some(24_000)),
        ("24–32K", Some(32_000)),
        ("≥32K", None),
    ];
    const OUTPUT_BUCKETS: &[(&str, Option<i64>)] = &[
        ("<512", Some(512)),
        ("512–1K", Some(1_000)),
        ("1–2K", Some(2_000)),
        ("2–4K", Some(4_000)),
        ("4–8K", Some(8_000)),
        ("8–20K", Some(20_000)),
        ("≥20K", None),
    ];

    let mut stmt = conn.prepare(
        "SELECT COALESCE(prompt_tokens, 0),
                COALESCE(completion_tokens, 0),
                COALESCE(done_reason, '')
         FROM llm_usage_logs
         WHERE ts >= ?1
           AND caller = 'bake'
           AND status = 'success'
           AND COALESCE(prompt_tokens, 0) >= 100",
    )?;
    let samples: Vec<(i64, i64, String)> = stmt
        .query_map(rusqlite::params![from_ms], |row| {
            Ok((row.get(0)?, row.get(1)?, row.get(2)?))
        })?
        .collect::<Result<_, _>>()?;

    let input_values: Vec<i64> = samples.iter().map(|sample| sample.0).collect();
    let output_values: Vec<i64> = samples.iter().map(|sample| sample.1).collect();
    Ok(BakeTokenDistribution {
        sample_count: samples.len() as i64,
        truncated_count: samples.iter().filter(|sample| sample.2 == "length").count() as i64,
        input_over_20k_count: input_values
            .iter()
            .filter(|tokens| **tokens >= 20_000)
            .count() as i64,
        input: build_token_metric_distribution(input_values, INPUT_BUCKETS),
        output: build_token_metric_distribution(output_values, OUTPUT_BUCKETS),
    })
}

#[derive(Debug, Serialize)]
pub struct CaptureFlow {
    pub today_count: i64,
    pub period_count: i64,
    pub eligible_count: i64,
    pub vectorized_count: i64,
    pub vectorization_rate: f64,
    pub knowledge_generated_count: i64,
    pub knowledge_generation_rate: f64,
    pub knowledge_linked_count: i64,
    pub knowledge_rate: f64,
    pub by_hour: Vec<HourCount>,
    pub by_app: Vec<AppCount>,
    pub recent: Vec<CaptureItem>,
}

#[derive(Debug, Serialize)]
pub struct CaptureItem {
    pub id: i64,
    pub ts: i64,
    pub app_name: String,
    pub win_title: String,
}

#[derive(Debug, Serialize)]
pub struct HourCount {
    pub hour: i64,
    pub count: i64,
}

#[derive(Debug, Serialize)]
pub struct AppCount {
    pub app: String,
    pub count: i64,
}

#[derive(Debug, Serialize)]
pub struct KnowledgeFlow {
    pub today_count: i64,
    pub period_count: i64,
    /// 采集与自动提炼总开关。关闭时库存仍可见，但等待时长不表示流水线故障。
    pub capture_enabled: bool,
    /// 全量库存：尚未归入 timeline 的可提炼 capture，不受趋势时间范围影响。
    pub pending_extraction_count: i64,
    pub oldest_pending_extraction_at_ms: Option<i64>,
    /// 提炼停摆告警：有待提炼 capture 且最老一条已等待超过 60 分钟。
    pub extraction_stalled: bool,
    /// 全量库存：已有 timeline、尚未生成任一 bake 产物的高价值候选。
    pub pending_bake_count: i64,
    pub oldest_pending_bake_at_ms: Option<i64>,
    /// 最近 6 小时折算的烘焙消化速率（候选/小时）。
    pub bake_drain_rate_per_hour: f64,
    /// 按当前速率预计清空积压所需时长；速率不可信时为 None。
    pub bake_eta_ms: Option<i64>,
    /// 已失败但仍在有界退避重试范围内的候选。
    pub bake_retry_pending_count: i64,
    pub bake_fresh_pending_count: i64,
    pub bake_retry_ready_count: i64,
    pub bake_retry_delayed_count: i64,
    pub bake_actionable_count: i64,
    /// 达到最大尝试次数、需要人工关注的候选。
    pub bake_retry_exhausted_count: i64,
    pub bake_timeout_failure_count: i64,
    pub bake_truncated_failure_count: i64,
    pub bake_other_failure_count: i64,
    pub bake_upstream_failure_count: i64,
    pub bake_recent_no_progress_count: i64,
    pub bake_next_retry_at_ms: Option<i64>,
    pub bake_recommended_retry_after_ms: i64,
    pub bake_scheduler_mismatch: bool,
    pub running_bake_count: i64,
    pub stale_bake_run_count: i64,
    pub by_time: Vec<KnowledgeTimePoint>,
    pub recent: Vec<KnowledgeItem>,
    /// 当前正在被提炼的 captures（来自 sidecar 实时状态）
    pub extracting: Vec<ExtractingCapture>,
    /// 最近一次成功提炼的时间戳（毫秒）；从未提炼则为 None
    pub last_extraction_at_ms: Option<i64>,
    /// 提炼器状态：running / waiting / idle / stalled
    pub extractor_status: String,
}

#[derive(Debug, Clone, Default)]
struct PipelineBacklogMetrics {
    pending_extraction_count: i64,
    oldest_pending_extraction_at_ms: Option<i64>,
    pending_bake_count: i64,
    oldest_pending_bake_at_ms: Option<i64>,
    bake_drain_rate_per_hour: f64,
    bake_eta_ms: Option<i64>,
    bake_retry_pending_count: i64,
    bake_fresh_pending_count: i64,
    bake_retry_ready_count: i64,
    bake_retry_delayed_count: i64,
    bake_actionable_count: i64,
    bake_retry_exhausted_count: i64,
    bake_timeout_failure_count: i64,
    bake_truncated_failure_count: i64,
    bake_other_failure_count: i64,
    bake_upstream_failure_count: i64,
    bake_recent_no_progress_count: i64,
    bake_next_retry_at_ms: Option<i64>,
    bake_recommended_retry_after_ms: i64,
    bake_scheduler_mismatch: bool,
    running_bake_count: i64,
    stale_bake_run_count: i64,
    bake_watermark_updated_at_ms: Option<i64>,
    latest_bake_status: Option<String>,
    recent_bake_run_count: i64,
    recent_bake_failed_count: i64,
    recent_bake_deferred_count: i64,
    recent_bake_runs_unhealthy: bool,
    bake_stalled: bool,
}

#[derive(Debug, Serialize, Clone)]
pub struct InferenceQueueMonitor {
    pub available: bool,
    pub idle: bool,
    pub queued_total: i64,
    pub queued_p0: i64,
    pub queued_p1: i64,
    pub queued_p2: i64,
    pub running_total: i64,
    pub running_p2: i64,
    pub oldest_wait_ms: i64,
    pub max_concurrency: i64,
    pub on_external_power: Option<bool>,
}

impl Default for InferenceQueueMonitor {
    fn default() -> Self {
        Self {
            available: false,
            idle: false,
            queued_total: 0,
            queued_p0: 0,
            queued_p1: 0,
            queued_p2: 0,
            running_total: 0,
            running_p2: 0,
            oldest_wait_ms: 0,
            max_concurrency: 0,
            on_external_power: None,
        }
    }
}

#[derive(Debug, Deserialize)]
struct UpstreamInferenceQueueResponse {
    status: String,
    idle: bool,
    stats: Option<UpstreamInferenceQueueStats>,
}

#[derive(Debug, Deserialize)]
struct UpstreamInferenceQueueStats {
    #[serde(default)]
    queue_lengths: HashMap<String, i64>,
    #[serde(default)]
    running_total: i64,
    #[serde(default)]
    running_by_lane: HashMap<String, i64>,
    #[serde(default)]
    oldest_wait_ms: i64,
    #[serde(default)]
    max_concurrency: i64,
    on_external_power: Option<bool>,
}

#[derive(Debug, Serialize, Clone)]
pub struct ExtractingCapture {
    pub id: i64,
    pub ts: i64,
    pub app_name: String,
    pub win_title: String,
    /// 所属提炼分组的开始时刻（sidecar 调用 _mark_group_extracting 的瞬间）。
    /// 用于前端显示「已提炼 Xs」，比 capture.ts 更精确（排除掉分组成熟前的等待时长）。
    pub group_started_at_ms: i64,
}

#[derive(Debug, Serialize, Clone)]
pub struct KnowledgeTimePoint {
    pub ts: i64,
    pub count: i64,
}

#[derive(Debug, Serialize)]
pub struct KnowledgeItem {
    pub id: i64,
    pub ts: i64,
    pub summary: String,
    pub category: String,
    pub importance: i64,
    pub app_name: String,
    pub win_title: String,
}

#[derive(Debug, Serialize)]
pub struct RagSessionStats {
    pub today_count: i64,
    pub period_count: i64,
    pub avg_latency_ms: i64,
    pub recent: Vec<RagSessionItem>,
}

#[derive(Debug, Serialize)]
pub struct RagSessionItem {
    pub id: i64,
    pub ts: i64,
    pub query: String,
    pub latency_ms: Option<i64>,
    pub context_count: i64,
}

#[derive(Debug, Serialize)]
pub struct TaskExecutionStats {
    pub total: i64,
    pub success: i64,
    pub failed: i64,
    pub success_rate: f64,
    pub recent: Vec<TaskExecutionItem>,
}

#[derive(Debug, Serialize)]
pub struct TaskExecutionItem {
    pub id: i64,
    pub task_name: String,
    pub status: String,
    pub started_at: i64,
    pub latency_ms: Option<i64>,
    pub knowledge_count: Option<i64>,
}

/// GET /api/monitor/overview
pub async fn monitor_overview(
    State(state): State<Arc<AppState>>,
    Query(params): Query<MonitorQuery>,
) -> Result<impl IntoResponse, ApiError> {
    let now_ms = Utc::now().timestamp_millis();
    let capture_enabled = state.is_capture_enabled();
    let range_ms = query_range_ms(&params);
    let from_ms = now_ms - range_ms;
    let today_start = local_day_start_ms(now_ms);
    let token_bucket_ms = trend_bucket_ms(range_ms);
    let knowledge_bucket_ms = knowledge_bucket_ms(range_ms);
    let fallback_noise_pattern = format!("{}%", FALLBACK_NOISE_OVERVIEW_PREFIX);
    let bake_queue_status = state
        .storage
        .get_bake_queue_status(MAX_BAKE_RETRY_FAILURES)
        .unwrap_or_default();

    let mut overview = state.storage.with_conn_async(move |conn| {
        let db_size_bytes = conn.query_row("PRAGMA page_count", [], |r| r.get::<_, i64>(0)).unwrap_or(0)
            * conn.query_row("PRAGMA page_size", [], |r| r.get::<_, i64>(0)).unwrap_or(0);
        let capture_total_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM captures",
            [],
            |r| r.get(0),
        ).unwrap_or(0);

        let total_period: i64 = conn.query_row(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage_logs WHERE ts >= ?1",
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);

        let total_today: i64 = conn.query_row(
            "SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage_logs WHERE ts >= ?1",
            rusqlite::params![today_start],
            |r| r.get(0),
        ).unwrap_or(0);

        let mut by_model_stmt = conn.prepare(
            "SELECT model_name,
                    COALESCE(SUM(total_tokens),0),
                    COALESCE(SUM(prompt_tokens),0),
                    COALESCE(SUM(completion_tokens),0),
                    COUNT(*)
             FROM llm_usage_logs WHERE ts >= ?1
             GROUP BY model_name ORDER BY COALESCE(SUM(total_tokens),0) DESC LIMIT 8"
        )?;
        let mut by_model: Vec<ModelUsage> = by_model_stmt
            .query_map(rusqlite::params![from_ms], |r| {
                Ok(ModelUsage {
                    model: r.get::<_, String>(0)?,
                    total: r.get::<_, i64>(1)?,
                    prompt: r.get::<_, i64>(2)?,
                    completion: r.get::<_, i64>(3)?,
                    calls: r.get::<_, i64>(4)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let mut by_caller_stmt = conn.prepare(
            "SELECT caller, COALESCE(SUM(total_tokens),0), COUNT(*)
             FROM llm_usage_logs WHERE ts >= ?1
             GROUP BY caller ORDER BY COALESCE(SUM(total_tokens),0) DESC LIMIT 8"
        )?;
        let by_caller = by_caller_stmt
            .query_map(rusqlite::params![from_ms], |r| {
                Ok(CallerUsage {
                    caller: r.get::<_, String>(0)?,
                    total: r.get::<_, i64>(1)?,
                    calls: r.get::<_, i64>(2)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let mut trend_stmt = conn.prepare(
            "SELECT (ts / ?1) * ?1 as bucket, COALESCE(SUM(total_tokens),0), COUNT(*)
             FROM llm_usage_logs WHERE ts >= ?2
             GROUP BY bucket ORDER BY bucket"
        )?;
        let trend = trend_stmt
            .query_map(rusqlite::params![token_bucket_ms, from_ms], |r| {
                let bucket_start: i64 = r.get(0)?;
                Ok(DayTrend {
                    ts: bucket_start + token_bucket_ms / 2,
                    date: bucket_label(range_ms, bucket_start),
                    tokens: r.get::<_, i64>(1)?,
                    calls: r.get::<_, i64>(2)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let mut trend_by_model: Vec<ModelTrend> = Vec::new();
        for item in by_model.iter() {
            let mut model_trend_stmt = conn.prepare(
                "SELECT (ts / ?1) * ?1 as bucket, COALESCE(SUM(total_tokens),0), COUNT(*)
                 FROM llm_usage_logs WHERE ts >= ?2 AND model_name = ?3
                 GROUP BY bucket ORDER BY bucket"
            )?;
            let model_trend = model_trend_stmt
                .query_map(rusqlite::params![token_bucket_ms, from_ms, &item.model], |r| {
                    let bucket_start: i64 = r.get(0)?;
                    Ok(DayTrend {
                        ts: bucket_start + token_bucket_ms / 2,
                        date: bucket_label(range_ms, bucket_start),
                        tokens: r.get::<_, i64>(1)?,
                        calls: r.get::<_, i64>(2)?,
                    })
                })?
                .filter_map(|r| r.ok())
                .collect();
            trend_by_model.push(ModelTrend {
                model: item.model.clone(),
                total: item.total,
                calls: item.calls,
                trend: model_trend,
            });
        }
        for item in &mut by_model {
            item.model = public_llm_model_name(&item.model);
        }
        for item in &mut trend_by_model {
            item.model = public_llm_model_name(&item.model);
        }
        let bake_distribution = load_bake_token_distribution(conn, from_ms)?;

        let today_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM captures WHERE ts >= ?1",
            rusqlite::params![today_start],
            |r| r.get(0),
        ).unwrap_or(0);
        let period_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM captures WHERE ts >= ?1",
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);
        // 口径见 doc/监控指标口径说明.md：可处理 captures = 非敏感且至少有一种
        // 可处理文本（ocr_text / ax_text）。早期版本误引用不存在的 is_noise /
        // vector_status 列导致查询报错被 unwrap_or(0) 吞掉，指标恒为 0。
        const ELIGIBLE_CAPTURE_PREDICATE: &str =
            "ts >= ?1 AND is_sensitive = 0 AND (COALESCE(ocr_text, '') != '' OR COALESCE(ax_text, '') != '')";
        let eligible_count: i64 = conn.query_row(
            &format!("SELECT COUNT(*) FROM captures WHERE {}", ELIGIBLE_CAPTURE_PREDICATE),
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);
        let vectorized_count: i64 = conn.query_row(
            &format!(
                "SELECT COUNT(DISTINCT captures.id)
                 FROM captures
                 JOIN vector_index ON vector_index.capture_id = captures.id
                 WHERE {}",
                ELIGIBLE_CAPTURE_PREDICATE
            ),
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);
        // 知识化率：可处理 captures 归属到的知识条目（timeline）数量。
        let knowledge_generated_count: i64 = conn.query_row(
            &format!(
                "SELECT COUNT(DISTINCT captures.timeline_id)
                 FROM captures
                 WHERE {} AND captures.timeline_id IS NOT NULL",
                ELIGIBLE_CAPTURE_PREDICATE
            ),
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);
        // 知识挂载率：已成功回填 timeline_id 的可处理 capture 数量。
        let knowledge_linked_count: i64 = conn.query_row(
            &format!(
                "SELECT COUNT(*) FROM captures WHERE {} AND timeline_id IS NOT NULL",
                ELIGIBLE_CAPTURE_PREDICATE
            ),
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);
        let vectorization_rate = if eligible_count > 0 {
            (vectorized_count as f64 / eligible_count as f64).min(1.0)
        } else { 0.0 };
        let knowledge_generation_rate = if eligible_count > 0 {
            (knowledge_generated_count as f64 / eligible_count as f64).min(1.0)
        } else { 0.0 };
        let knowledge_rate = if eligible_count > 0 {
            (knowledge_linked_count as f64 / eligible_count as f64).min(1.0)
        } else { 0.0 };

        let mut by_hour_stmt = conn.prepare(
            "SELECT CAST(strftime('%H', datetime(ts/1000, 'unixepoch', 'localtime')) AS INTEGER), COUNT(*)
             FROM captures WHERE ts >= ?1
             GROUP BY 1 ORDER BY 1"
        )?;
        let by_hour = by_hour_stmt
            .query_map(rusqlite::params![from_ms], |r| {
                Ok(HourCount { hour: r.get(0)?, count: r.get(1)? })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let mut by_app_stmt = conn.prepare(
            "SELECT COALESCE(app_name, '未知'), COUNT(*)
             FROM captures WHERE ts >= ?1
             GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 8"
        )?;
        let by_app = by_app_stmt
            .query_map(rusqlite::params![from_ms], |r| {
                Ok(AppCount { app: r.get(0)?, count: r.get(1)? })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let mut recent_capture_stmt = conn.prepare(
            "SELECT id, ts, COALESCE(app_name, ''), COALESCE(win_title, '')
             FROM captures WHERE ts >= ?1 ORDER BY ts DESC LIMIT 10"
        )?;
        let recent = recent_capture_stmt
            .query_map(rusqlite::params![from_ms], |r| {
                Ok(CaptureItem {
                    id: r.get(0)?,
                    ts: r.get(1)?,
                    app_name: r.get(2)?,
                    win_title: r.get(3)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let knowledge_today_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM timelines WHERE created_at >= datetime(?1/1000, 'unixepoch') AND summary NOT LIKE ?2 AND COALESCE(is_self_generated, 0) = 0",
            rusqlite::params![today_start, fallback_noise_pattern.as_str()],
            |r| r.get(0),
        ).unwrap_or(0);
        let knowledge_period_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM timelines WHERE created_at >= datetime(?1/1000, 'unixepoch') AND summary NOT LIKE ?2 AND COALESCE(is_self_generated, 0) = 0",
            rusqlite::params![from_ms, fallback_noise_pattern.as_str()],
            |r| r.get(0),
        ).unwrap_or(0);
        let backlog = load_pipeline_backlog_metrics(
            conn,
            now_ms,
            capture_enabled,
            &bake_queue_status,
        )
        .unwrap_or_default();

        let mut knowledge_by_time_stmt = conn.prepare(
            "SELECT (CAST(strftime('%s', created_at) AS INTEGER) * 1000 / ?1) * ?1 + ?1/2 as bucket, COUNT(*)
             FROM timelines
             WHERE created_at >= datetime(?2/1000, 'unixepoch')
               AND summary NOT LIKE ?3
               AND COALESCE(is_self_generated, 0) = 0
             GROUP BY bucket ORDER BY bucket"
        )?;
        let knowledge_by_time = knowledge_by_time_stmt
            .query_map(rusqlite::params![knowledge_bucket_ms, from_ms, fallback_noise_pattern.as_str()], |r| {
                Ok(KnowledgeTimePoint { ts: r.get(0)?, count: r.get(1)? })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let mut recent_knowledge_stmt = conn.prepare(
            "SELECT id,
                    CAST(strftime('%s', created_at) AS INTEGER) * 1000,
                    COALESCE(summary, ''),
                    COALESCE(category, ''),
                    COALESCE(importance, 0),
                    COALESCE(frag_app_name, ''),
                    COALESCE(frag_win_title, '')
             FROM timelines
             WHERE created_at >= datetime(?1/1000, 'unixepoch')
               AND summary NOT LIKE ?2
               AND COALESCE(is_self_generated, 0) = 0
             ORDER BY created_at DESC LIMIT 10"
        )?;
        let knowledge_recent = recent_knowledge_stmt
            .query_map(rusqlite::params![from_ms, fallback_noise_pattern.as_str()], |r| {
                Ok(KnowledgeItem {
                    id: r.get(0)?,
                    ts: r.get(1)?,
                    summary: r.get(2)?,
                    category: r.get(3)?,
                    importance: r.get(4)?,
                    app_name: r.get(5)?,
                    win_title: r.get(6)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let rag_today_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM rag_sessions WHERE ts >= ?1",
            rusqlite::params![today_start],
            |r| r.get(0),
        ).unwrap_or(0);
        let rag_period_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM rag_sessions WHERE ts >= ?1",
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);
        let avg_latency_ms: i64 = conn.query_row(
            "SELECT COALESCE(AVG(latency_ms), 0) FROM rag_sessions WHERE ts >= ?1 AND latency_ms IS NOT NULL",
            rusqlite::params![from_ms],
            |r| r.get::<_, f64>(0).map(|v| v.round() as i64),
        ).unwrap_or(0);

        let mut rag_recent_stmt = conn.prepare(
            "SELECT id, ts, COALESCE(user_query, ''), latency_ms,
                    COALESCE(json_array_length(retrieved_ids), 0)
             FROM rag_sessions WHERE ts >= ?1 ORDER BY ts DESC LIMIT 10"
        )?;
        let rag_recent = rag_recent_stmt
            .query_map(rusqlite::params![from_ms], |r| {
                Ok(RagSessionItem {
                    id: r.get(0)?,
                    ts: r.get(1)?,
                    query: r.get(2)?,
                    latency_ms: r.get(3)?,
                    context_count: r.get(4)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();

        let total_exec: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_executions WHERE started_at >= ?1",
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);
        let success_exec: i64 = conn.query_row(
            "SELECT COUNT(*) FROM task_executions WHERE started_at >= ?1 AND status = 'success'",
            rusqlite::params![from_ms],
            |r| r.get(0),
        ).unwrap_or(0);
        let failed_exec = total_exec.saturating_sub(success_exec);
        let success_rate = if total_exec > 0 {
            success_exec as f64 * 100.0 / total_exec as f64
        } else { 0.0 };

        let mut exec_recent_stmt = conn.prepare(
            "SELECT te.id, COALESCE(st.name, ''), COALESCE(te.status, ''), te.started_at, te.latency_ms, te.knowledge_count
             FROM task_executions te
             LEFT JOIN scheduled_tasks st ON st.id = te.task_id
             WHERE te.started_at >= ?1
             ORDER BY te.started_at DESC LIMIT 10"
        )?;
        let recent_exec = exec_recent_stmt
            .query_map(rusqlite::params![from_ms], |r| {
                Ok(TaskExecutionItem {
                    id: r.get(0)?,
                    task_name: r.get(1)?,
                    status: r.get(2)?,
                    started_at: r.get(3)?,
                    latency_ms: r.get(4)?,
                    knowledge_count: r.get(5)?,
                })
            })?
            .filter_map(|r| r.ok())
            .collect();

        Ok(MonitorOverview {
            db_size_bytes,
            capture_total_count,
            service_health: ServiceHealth::unknown("Sidecar 运行时状态尚未读取"),
            token_usage: TokenUsage {
                total_period,
                total_today,
                by_model,
                by_caller,
                trend,
                trend_by_model,
                bake_distribution,
            },
            ocr_backfill: ocr_backfill_metrics_snapshot(range_ms, now_ms),
            capture_flow: CaptureFlow {
                today_count,
                period_count,
                eligible_count,
                vectorized_count,
                vectorization_rate,
                knowledge_generated_count,
                knowledge_generation_rate,
                knowledge_linked_count,
                knowledge_rate,
                by_hour,
                by_app,
                recent,
            },
            knowledge_flow: KnowledgeFlow {
                today_count: knowledge_today_count,
                period_count: knowledge_period_count,
                capture_enabled,
                pending_extraction_count: backlog.pending_extraction_count,
                oldest_pending_extraction_at_ms: backlog.oldest_pending_extraction_at_ms,
                extraction_stalled: backlog.pending_extraction_count > 0
                    && backlog
                        .oldest_pending_extraction_at_ms
                        .map(|ts| now_ms.saturating_sub(ts) > 60 * 60 * 1000)
                        .unwrap_or(false),
                pending_bake_count: backlog.pending_bake_count,
                oldest_pending_bake_at_ms: backlog.oldest_pending_bake_at_ms,
                bake_drain_rate_per_hour: backlog.bake_drain_rate_per_hour,
                bake_eta_ms: backlog.bake_eta_ms,
                bake_retry_pending_count: backlog.bake_retry_pending_count,
                bake_fresh_pending_count: backlog.bake_fresh_pending_count,
                bake_retry_ready_count: backlog.bake_retry_ready_count,
                bake_retry_delayed_count: backlog.bake_retry_delayed_count,
                bake_actionable_count: backlog.bake_actionable_count,
                bake_retry_exhausted_count: backlog.bake_retry_exhausted_count,
                bake_timeout_failure_count: backlog.bake_timeout_failure_count,
                bake_truncated_failure_count: backlog.bake_truncated_failure_count,
                bake_other_failure_count: backlog.bake_other_failure_count,
                bake_upstream_failure_count: backlog.bake_upstream_failure_count,
                bake_recent_no_progress_count: backlog.bake_recent_no_progress_count,
                bake_next_retry_at_ms: backlog.bake_next_retry_at_ms,
                bake_recommended_retry_after_ms: backlog.bake_recommended_retry_after_ms,
                bake_scheduler_mismatch: backlog.bake_scheduler_mismatch,
                running_bake_count: backlog.running_bake_count,
                stale_bake_run_count: backlog.stale_bake_run_count,
                by_time: knowledge_by_time,
                recent: knowledge_recent,
                extracting: Vec::new(),
                last_extraction_at_ms: None,
                extractor_status: "stalled".to_string(),
            },
            rag_sessions: RagSessionStats {
                today_count: rag_today_count,
                period_count: rag_period_count,
                avg_latency_ms,
                recent: rag_recent,
            },
            task_executions: TaskExecutionStats {
                total: total_exec,
                success: success_exec,
                failed: failed_exec,
                success_rate,
                recent: recent_exec,
            },
        })
    }).await?;

    enrich_extractor_status(&mut overview.knowledge_flow, now_ms).await;
    overview.service_health = read_sidecar_runtime_health(now_ms).await;

    Ok(Json(overview))
}

async fn read_sidecar_runtime_health(now_ms: i64) -> ServiceHealth {
    const RUNTIME_STATUS_STALENESS_MS: i64 = 5 * 60 * 1000;

    let path = match std::env::var_os("HOME") {
        Some(home) => std::path::PathBuf::from(home)
            .join(".memory-bread")
            .join("state")
            .join("sidecar_runtime_status.json"),
        None => return ServiceHealth::unknown("无法定位 HOME，不能读取 sidecar 运行状态"),
    };

    #[derive(Deserialize)]
    struct RuntimeStatusFile {
        #[serde(default)]
        mode: String,
        #[serde(default)]
        full_dispatch_ready: bool,
        #[serde(default)]
        background_processor_running: bool,
        #[serde(default)]
        critical_checks_passed: bool,
        #[serde(default)]
        embedding_ok: bool,
        #[serde(default)]
        issues: Vec<String>,
        #[serde(default)]
        updated_at_ms: Option<i64>,
    }

    let read_path = path.clone();
    let read_result = tokio::task::spawn_blocking(move || std::fs::read(&read_path)).await;
    let bytes = match read_result {
        Ok(Ok(b)) => b,
        _ => return ServiceHealth::unknown("Sidecar 未写入运行时状态，完整后台能力可能未启动"),
    };

    let body: RuntimeStatusFile = match serde_json::from_slice(&bytes) {
        Ok(b) => b,
        Err(_) => return ServiceHealth::unknown("Sidecar 运行时状态文件无法解析"),
    };

    let stale = body
        .updated_at_ms
        .map(|updated| now_ms.saturating_sub(updated) > RUNTIME_STATUS_STALENESS_MS)
        .unwrap_or(true);

    let mut issues = body.issues;
    if stale {
        issues.push("Sidecar 运行时状态超过 5 分钟未更新".to_string());
    }

    let status = service_health_status(
        stale,
        body.critical_checks_passed,
        body.full_dispatch_ready,
        body.background_processor_running,
        body.embedding_ok,
        !issues.is_empty(),
    );

    ServiceHealth {
        status: status.to_string(),
        mode: if body.mode.is_empty() {
            "unknown".to_string()
        } else {
            body.mode
        },
        full_dispatch_ready: body.full_dispatch_ready,
        background_processor_running: body.background_processor_running,
        critical_checks_passed: body.critical_checks_passed,
        embedding_ok: body.embedding_ok,
        issues,
        updated_at_ms: body.updated_at_ms,
    }
}

fn service_health_status(
    stale: bool,
    critical_checks_passed: bool,
    full_dispatch_ready: bool,
    background_processor_running: bool,
    embedding_ok: bool,
    has_issues: bool,
) -> &'static str {
    if !critical_checks_passed || !full_dispatch_ready {
        "down"
    } else if stale || !background_processor_running || !embedding_ok || has_issues {
        "degraded"
    } else {
        "ok"
    }
}

/// 直接读取 sidecar 写入的 ~/.memory-bread/state/extraction_status.json，
/// 并据此推导 extractor_status 文案。
///
/// 选择文件而不是 HTTP：sidecar Flask 与 OCR/Paddle 在同一进程，
/// 重负载时 GIL/socket 抢占会让 HTTP 探测假死，造成误报 stalled。
/// 文件路径走 OS page cache，几乎零延迟，且 sidecar 即使卡住，
/// 我们仍能拿到它最后一次落盘的真实状态。
async fn enrich_extractor_status(flow: &mut KnowledgeFlow, now_ms: i64) {
    const RECENT_ACTIVITY_WINDOW_MS: i64 = 5 * 60 * 1000;
    // 文件超过 15 分钟没更新才认为 sidecar 真的死了：
    // BackgroundProcessor 每次扫描（30s 间隔）会通过 mark/unmark 触发写入，
    // 即使分组未成熟也会因为下一轮发现 pending 而再次触达此处。
    const STATUS_FILE_STALENESS_MS: i64 = 15 * 60 * 1000;

    if !flow.capture_enabled {
        flow.extracting.clear();
        flow.extractor_status = "paused".to_string();
        return;
    }

    let path = match std::env::var_os("HOME") {
        Some(home) => std::path::PathBuf::from(home)
            .join(".memory-bread")
            .join("state")
            .join("extraction_status.json"),
        None => {
            flow.extractor_status = "stalled".to_string();
            return;
        }
    };

    #[derive(Deserialize)]
    struct SidecarExtracting {
        id: i64,
        ts: i64,
        #[serde(default)]
        app_name: String,
        #[serde(default)]
        win_title: String,
    }

    #[derive(Deserialize)]
    struct SidecarGroup {
        #[serde(default)]
        started_at_ms: i64,
        #[serde(default)]
        captures: Vec<SidecarExtracting>,
    }

    #[derive(Deserialize)]
    struct SidecarStatus {
        #[serde(default)]
        running: bool,
        #[serde(default)]
        extracting_captures: Vec<SidecarExtracting>,
        #[serde(default)]
        extracting_groups: Vec<SidecarGroup>,
        #[serde(default)]
        last_extraction_at_ms: Option<i64>,
        #[serde(default)]
        updated_at_ms: Option<i64>,
    }

    let read_path = path.clone();
    let read_result = tokio::task::spawn_blocking(move || std::fs::read(&read_path)).await;

    let bytes = match read_result {
        Ok(Ok(b)) => b,
        _ => {
            // 文件不存在或读失败：sidecar 后台处理器从未启动过
            flow.extractor_status = if flow.pending_extraction_count > 0 {
                "stalled".to_string()
            } else {
                "idle".to_string()
            };
            return;
        }
    };

    let body: SidecarStatus = match serde_json::from_slice(&bytes) {
        Ok(b) => b,
        Err(_) => {
            flow.extractor_status = "stalled".to_string();
            return;
        }
    };

    // 优先消费 extracting_groups（含 group started_at_ms），fallback 到平铺 captures
    flow.extracting = if !body.extracting_groups.is_empty() {
        body.extracting_groups
            .into_iter()
            .flat_map(|g| {
                let started = g.started_at_ms;
                g.captures.into_iter().map(move |c| ExtractingCapture {
                    id: c.id,
                    ts: c.ts,
                    app_name: c.app_name,
                    win_title: c.win_title,
                    group_started_at_ms: started,
                })
            })
            .collect()
    } else {
        body.extracting_captures
            .into_iter()
            .map(|c| ExtractingCapture {
                id: c.id,
                ts: c.ts,
                app_name: c.app_name,
                win_title: c.win_title,
                group_started_at_ms: 0,
            })
            .collect()
    };
    flow.last_extraction_at_ms = body.last_extraction_at_ms;

    // 状态判定优先级：
    //   1. 当前有 group 在提炼 → running
    //   2. running=false（sidecar 标识自己未启动）→ stalled
    //   3. 最近 5min 内有成功提炼 → running
    //   4. 文件超过 15min 没更新且 pending>0 → stalled
    //   5. pending=0 → idle
    //   6. 其他 → waiting（在线但片段未成熟）
    flow.extractor_status = if !flow.extracting.is_empty() {
        "running".to_string()
    } else if !body.running {
        "stalled".to_string()
    } else if let Some(last_ms) = flow.last_extraction_at_ms {
        if now_ms.saturating_sub(last_ms) <= RECENT_ACTIVITY_WINDOW_MS {
            "running".to_string()
        } else if let Some(updated_ms) = body.updated_at_ms {
            if now_ms.saturating_sub(updated_ms) > STATUS_FILE_STALENESS_MS
                && flow.pending_extraction_count > 0
            {
                "stalled".to_string()
            } else if flow.pending_extraction_count > 0 {
                "waiting".to_string()
            } else {
                "idle".to_string()
            }
        } else if flow.pending_extraction_count > 0 {
            "waiting".to_string()
        } else {
            "idle".to_string()
        }
    } else if let Some(updated_ms) = body.updated_at_ms {
        if now_ms.saturating_sub(updated_ms) > STATUS_FILE_STALENESS_MS
            && flow.pending_extraction_count > 0
        {
            "stalled".to_string()
        } else if flow.pending_extraction_count > 0 {
            "waiting".to_string()
        } else {
            "idle".to_string()
        }
    } else if flow.pending_extraction_count > 0 {
        "waiting".to_string()
    } else {
        "idle".to_string()
    };
}

// ── 实时提炼状态轻量端点 ─────────────────────────────────────────────────────
//
// GET /api/monitor/extraction_live
// 专为「最近知识提炼记录」卡片高频轮询（3s）设计：
// - 不计算 token / capture 趋势，不分组聚合，只查必要的 recent 行
// - payload 体积约为 overview 的 1/30，便于浏览器侧 setState 不触发整页重渲染
// - extracting / last_extraction_at_ms / extractor_status 走 status.json，sidecar 卡顿也不会假死

#[derive(Debug, Serialize)]
pub struct ExtractionLiveResponse {
    pub extractor_status: String,
    pub capture_enabled: bool,
    pub service_health: ServiceHealth,
    pub extracting: Vec<ExtractingCapture>,
    pub last_extraction_at_ms: Option<i64>,
    pub pending_extraction_count: i64,
    pub oldest_pending_extraction_at_ms: Option<i64>,
    pub pending_bake_count: i64,
    pub oldest_pending_bake_at_ms: Option<i64>,
    pub bake_drain_rate_per_hour: f64,
    pub bake_eta_ms: Option<i64>,
    pub bake_retry_pending_count: i64,
    pub bake_fresh_pending_count: i64,
    pub bake_retry_ready_count: i64,
    pub bake_retry_delayed_count: i64,
    pub bake_actionable_count: i64,
    pub bake_retry_exhausted_count: i64,
    pub bake_timeout_failure_count: i64,
    pub bake_truncated_failure_count: i64,
    pub bake_other_failure_count: i64,
    pub bake_upstream_failure_count: i64,
    pub bake_recent_no_progress_count: i64,
    pub bake_next_retry_at_ms: Option<i64>,
    pub bake_recommended_retry_after_ms: i64,
    pub bake_scheduler_mismatch: bool,
    pub running_bake_count: i64,
    pub stale_bake_run_count: i64,
    pub bake_watermark_updated_at_ms: Option<i64>,
    pub latest_bake_status: Option<String>,
    pub recent_bake_run_count: i64,
    pub recent_bake_failed_count: i64,
    pub recent_bake_deferred_count: i64,
    pub recent_bake_runs_unhealthy: bool,
    pub bake_stalled: bool,
    pub inference_queue: InferenceQueueMonitor,
    pub recent: Vec<KnowledgeItem>,
    pub server_now_ms: i64,
}

pub async fn monitor_extraction_live(
    State(state): State<Arc<AppState>>,
    Query(params): Query<MonitorQuery>,
) -> Result<impl IntoResponse, ApiError> {
    let now_ms = Utc::now().timestamp_millis();
    let capture_enabled = state.is_capture_enabled();
    let from_ms = now_ms - query_range_ms(&params);
    let fallback_noise_pattern = format!("{}%", FALLBACK_NOISE_OVERVIEW_PREFIX);
    let bake_queue_status = state
        .storage
        .get_bake_queue_status(MAX_BAKE_RETRY_FAILURES)
        .unwrap_or_default();
    let bake_queue_status_for_refresh = bake_queue_status.clone();
    let (mut backlog, recent) = state
        .storage
        .with_conn_async(move |conn| {
            let backlog =
                load_pipeline_backlog_metrics(conn, now_ms, capture_enabled, &bake_queue_status)
                    .unwrap_or_default();

            let mut stmt = conn.prepare(
                "SELECT id,
                        CAST(strftime('%s', created_at) AS INTEGER) * 1000,
                        COALESCE(summary, ''),
                        COALESCE(category, ''),
                        COALESCE(importance, 0),
                        COALESCE(frag_app_name, ''),
                        COALESCE(frag_win_title, '')
                 FROM timelines
                 WHERE created_at >= datetime(?1/1000, 'unixepoch')
                   AND summary NOT LIKE ?2
                 ORDER BY created_at DESC LIMIT 10",
            )?;
            let recent: Vec<KnowledgeItem> = stmt
                .query_map(
                    rusqlite::params![from_ms, fallback_noise_pattern.as_str()],
                    |r| {
                        Ok(KnowledgeItem {
                            id: r.get(0)?,
                            ts: r.get(1)?,
                            summary: r.get(2)?,
                            category: r.get(3)?,
                            importance: r.get(4)?,
                            app_name: r.get(5)?,
                            win_title: r.get(6)?,
                        })
                    },
                )?
                .filter_map(|r| r.ok())
                .collect();

            Ok((backlog, recent))
        })
        .await?;

    // 监控已经识别出陈旧 run 时就地自愈，但仅在确有 stale 记录时写库，
    // 避免 3 秒轮询退化成持续 SQLite 写事务。
    if backlog.stale_bake_run_count > 0 {
        match state.storage.fail_stale_running_bake_runs() {
            Ok(recovered) if recovered > 0 => {
                tracing::warn!("监控轮询已收敛 {} 个陈旧 running bake run", recovered);
                backlog = state
                    .storage
                    .with_conn_async(move |conn| {
                        Ok(load_pipeline_backlog_metrics(
                            conn,
                            now_ms,
                            capture_enabled,
                            &bake_queue_status_for_refresh,
                        )?)
                    })
                    .await?;
            }
            Err(error) => {
                tracing::warn!("监控轮询收敛陈旧 bake run 失败: {}", error);
            }
            _ => {}
        }
    }

    // 复用 enrich_extractor_status 的状态判定逻辑：把字段塞进临时 KnowledgeFlow 跑一遍
    let mut tmp = KnowledgeFlow {
        today_count: 0,
        period_count: 0,
        capture_enabled,
        pending_extraction_count: backlog.pending_extraction_count,
        oldest_pending_extraction_at_ms: backlog.oldest_pending_extraction_at_ms,
        extraction_stalled: backlog.pending_extraction_count > 0
            && backlog
                .oldest_pending_extraction_at_ms
                .map(|ts| now_ms.saturating_sub(ts) > 60 * 60 * 1000)
                .unwrap_or(false),
        pending_bake_count: backlog.pending_bake_count,
        oldest_pending_bake_at_ms: backlog.oldest_pending_bake_at_ms,
        bake_drain_rate_per_hour: backlog.bake_drain_rate_per_hour,
        bake_eta_ms: backlog.bake_eta_ms,
        bake_retry_pending_count: backlog.bake_retry_pending_count,
        bake_fresh_pending_count: backlog.bake_fresh_pending_count,
        bake_retry_ready_count: backlog.bake_retry_ready_count,
        bake_retry_delayed_count: backlog.bake_retry_delayed_count,
        bake_actionable_count: backlog.bake_actionable_count,
        bake_retry_exhausted_count: backlog.bake_retry_exhausted_count,
        bake_timeout_failure_count: backlog.bake_timeout_failure_count,
        bake_truncated_failure_count: backlog.bake_truncated_failure_count,
        bake_other_failure_count: backlog.bake_other_failure_count,
        bake_upstream_failure_count: backlog.bake_upstream_failure_count,
        bake_recent_no_progress_count: backlog.bake_recent_no_progress_count,
        bake_next_retry_at_ms: backlog.bake_next_retry_at_ms,
        bake_recommended_retry_after_ms: backlog.bake_recommended_retry_after_ms,
        bake_scheduler_mismatch: backlog.bake_scheduler_mismatch,
        running_bake_count: backlog.running_bake_count,
        stale_bake_run_count: backlog.stale_bake_run_count,
        by_time: Vec::new(),
        recent: Vec::new(),
        extracting: Vec::new(),
        last_extraction_at_ms: None,
        extractor_status: "stalled".to_string(),
    };
    enrich_extractor_status(&mut tmp, now_ms).await;
    let mut service_health = read_sidecar_runtime_health(now_ms).await;
    let inference_queue = read_inference_queue_monitor(&state.sidecar_url).await;
    apply_bake_pipeline_health(&mut service_health, &backlog);

    Ok(Json(ExtractionLiveResponse {
        extractor_status: tmp.extractor_status,
        capture_enabled,
        service_health,
        extracting: tmp.extracting,
        last_extraction_at_ms: tmp.last_extraction_at_ms,
        pending_extraction_count: backlog.pending_extraction_count,
        oldest_pending_extraction_at_ms: backlog.oldest_pending_extraction_at_ms,
        pending_bake_count: backlog.pending_bake_count,
        oldest_pending_bake_at_ms: backlog.oldest_pending_bake_at_ms,
        bake_drain_rate_per_hour: backlog.bake_drain_rate_per_hour,
        bake_eta_ms: backlog.bake_eta_ms,
        bake_retry_pending_count: backlog.bake_retry_pending_count,
        bake_fresh_pending_count: backlog.bake_fresh_pending_count,
        bake_retry_ready_count: backlog.bake_retry_ready_count,
        bake_retry_delayed_count: backlog.bake_retry_delayed_count,
        bake_actionable_count: backlog.bake_actionable_count,
        bake_retry_exhausted_count: backlog.bake_retry_exhausted_count,
        bake_timeout_failure_count: backlog.bake_timeout_failure_count,
        bake_truncated_failure_count: backlog.bake_truncated_failure_count,
        bake_other_failure_count: backlog.bake_other_failure_count,
        bake_upstream_failure_count: backlog.bake_upstream_failure_count,
        bake_recent_no_progress_count: backlog.bake_recent_no_progress_count,
        bake_next_retry_at_ms: backlog.bake_next_retry_at_ms,
        bake_recommended_retry_after_ms: backlog.bake_recommended_retry_after_ms,
        bake_scheduler_mismatch: backlog.bake_scheduler_mismatch,
        running_bake_count: backlog.running_bake_count,
        stale_bake_run_count: backlog.stale_bake_run_count,
        bake_watermark_updated_at_ms: backlog.bake_watermark_updated_at_ms,
        latest_bake_status: backlog.latest_bake_status,
        recent_bake_run_count: backlog.recent_bake_run_count,
        recent_bake_failed_count: backlog.recent_bake_failed_count,
        recent_bake_deferred_count: backlog.recent_bake_deferred_count,
        recent_bake_runs_unhealthy: backlog.recent_bake_runs_unhealthy,
        bake_stalled: backlog.bake_stalled,
        inference_queue,
        recent,
        server_now_ms: now_ms,
    }))
}

// ── 系统资源响应结构 ──────────────────────────────────────────────────────────

#[derive(Debug, Serialize)]
pub struct SystemResourcesResponse {
    pub db_size_bytes: i64,
    pub trends: ResourceTrends,
    pub gpu_trend: Vec<MetricPoint>,
    pub model_gpu_trend: Vec<MetricPoint>,
    pub disk_trend: Vec<DiskPoint>,
    pub knowledge_events: Vec<KnowledgeTimePoint>,
    pub model_events: Vec<ModelEventItem>,
    pub model_runtime_breakdown: Vec<ModelRuntimeBreakdownItem>,
    pub latest: ResourceLatestBundle,
}

#[derive(Debug, Serialize)]
pub struct ResourceTrends {
    pub system_cpu: Vec<MetricPoint>,
    pub system_mem: Vec<MetricPoint>,
    pub suite_cpu: Vec<MetricPoint>,
    pub suite_mem: Vec<MetricPoint>,
    pub model_cpu: Vec<MetricPoint>,
    pub model_mem: Vec<MetricPoint>,
    pub model_cpu_series: Vec<NamedMetricSeries>,
    pub model_mem_series: Vec<NamedMetricSeries>,
    pub model_estimated_mem_series: Vec<NamedMetricSeries>,
}

#[derive(Debug, Serialize, Clone)]
pub struct MetricPoint {
    pub ts: i64,
    pub value: f64,
}

#[derive(Debug, Serialize, Clone)]
pub struct NamedMetricSeries {
    pub key: String,
    pub label: String,
    pub points: Vec<MetricPoint>,
    pub process_names: Vec<String>,
    pub coverage_status: Option<String>,
    pub coverage_note: Option<String>,
}

#[derive(Debug, Serialize, Clone)]
pub struct DiskPoint {
    pub ts: i64,
    pub read_mb: f64,
    pub write_mb: f64,
}

#[derive(Debug, Serialize)]
pub struct ModelEventItem {
    pub ts: i64,
    pub event_type: String,
    pub model_type: String,
    pub model_name: String,
    pub duration_ms: Option<i64>,
    pub memory_mb: Option<i64>,
    pub mem_before_mb: Option<i64>,
    pub mem_after_mb: Option<i64>,
    pub error_msg: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ModelRuntimeBreakdownItem {
    pub key: String,
    pub label: String,
    pub cpu_percent: f64,
    pub mem_process_mb: i64,
    pub process_count: usize,
    pub coverage_status: Option<String>,
    pub coverage_note: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ResourceLatestBundle {
    pub system: Option<SystemLatestMetrics>,
    pub suite: Option<ScopedLatestMetrics>,
    pub model: Option<ScopedLatestMetrics>,
}

#[derive(Debug, Serialize)]
pub struct SystemLatestMetrics {
    pub cpu_total: f64,
    pub mem_total_mb: i64,
    pub mem_used_mb: i64,
    pub mem_percent: f64,
    pub gpu_percent: Option<f64>,
    pub gpu_name: Option<String>,
    pub gpu_total_label: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ScopedLatestMetrics {
    pub cpu_percent: f64,
    pub mem_process_mb: i64,
    pub process_count: usize,
    pub process_names: Vec<String>,
    pub coverage_status: Option<String>,
    pub coverage_note: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct SystemQuery {
    #[serde(default = "default_sys_range")]
    pub range: String,
}

fn default_sys_range() -> String {
    "6h".to_string()
}

fn has_column(columns: &HashSet<String>, name: &str) -> bool {
    columns.contains(name)
}

fn runtime_label(key: &str) -> String {
    match key {
        "sidecar_local_runtime" => "AI Sidecar 本地运行时".to_string(),
        "model_api_runtime" => "Model API / RAG 运行时".to_string(),
        "ollama_runtime" => "本地 AI 引擎".to_string(),
        _ => key.to_string(),
    }
}

fn sidecar_model_type_label(model_type: &str, model_name: &str) -> String {
    if model_name.contains('·') {
        return model_name.to_string();
    }
    match model_type {
        "ocr" => format!("OCR · {model_name}"),
        "embedding" => "Embedding · MBEMB V1.0".to_string(),
        "asr" => format!("ASR · {model_name}"),
        "vlm" => format!("VLM · {model_name}"),
        "llm" => format!("LLM · {}", public_llm_model_name(model_name)),
        _ => model_name.to_string(),
    }
}

fn has_scope_columns(columns: &HashSet<String>) -> bool {
    [
        "scope",
        "source",
        "target_pids_json",
        "coverage_status",
        "coverage_note",
    ]
    .iter()
    .all(|column| has_column(columns, column))
}

fn downsample_metric_points(points: Vec<MetricPoint>, max_points: usize) -> Vec<MetricPoint> {
    if points.len() <= max_points || max_points == 0 {
        return points;
    }

    let last_index = points.len() - 1;
    let mut sampled = Vec::with_capacity(max_points + 1);
    for i in 0..max_points {
        let index = i * last_index / (max_points - 1).max(1);
        let point = points[index].clone();
        if sampled.last().map(|item: &MetricPoint| item.ts) != Some(point.ts) {
            sampled.push(point);
        }
    }
    if sampled.last().map(|item| item.ts) != Some(points[last_index].ts) {
        sampled.push(points[last_index].clone());
    }
    sampled
}

fn downsample_named_metric_series(
    series: Vec<NamedMetricSeries>,
    max_points: usize,
) -> Vec<NamedMetricSeries> {
    series
        .into_iter()
        .map(|mut item| {
            item.points = downsample_metric_points(item.points, max_points);
            item
        })
        .collect()
}

fn downsample_disk_points(points: Vec<DiskPoint>, max_points: usize) -> Vec<DiskPoint> {
    if points.len() <= max_points || max_points == 0 {
        return points;
    }

    let last_index = points.len() - 1;
    let mut sampled = Vec::with_capacity(max_points + 1);
    for i in 0..max_points {
        let index = i * last_index / (max_points - 1).max(1);
        let point = points[index].clone();
        if sampled.last().map(|item: &DiskPoint| item.ts) != Some(point.ts) {
            sampled.push(point);
        }
    }
    if sampled.last().map(|item| item.ts) != Some(points[last_index].ts) {
        sampled.push(points[last_index].clone());
    }
    sampled
}

fn downsample_knowledge_points(
    points: Vec<KnowledgeTimePoint>,
    max_points: usize,
) -> Vec<KnowledgeTimePoint> {
    if points.len() <= max_points || max_points == 0 {
        return points;
    }

    let last_index = points.len() - 1;
    let mut sampled = Vec::with_capacity(max_points + 1);
    for i in 0..max_points {
        let index = i * last_index / (max_points - 1).max(1);
        let point = points[index].clone();
        if sampled.last().map(|item: &KnowledgeTimePoint| item.ts) != Some(point.ts) {
            sampled.push(point);
        }
    }
    if sampled.last().map(|item| item.ts) != Some(points[last_index].ts) {
        sampled.push(points[last_index].clone());
    }
    sampled
}

fn scoped_metric_trend(
    conn: &rusqlite::Connection,
    scope: &str,
    metric_expr: &str,
    bucket_ms: i64,
    from_ms: i64,
) -> Result<Vec<MetricPoint>, rusqlite::Error> {
    let sql = format!(
        "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, AVG({metric_expr})
         FROM system_metrics
         WHERE ts >= ?2 AND scope = ?3
         GROUP BY bucket ORDER BY bucket"
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params![bucket_ms, from_ms, scope], |r| {
        Ok(MetricPoint {
            ts: r.get(0)?,
            value: r.get(1)?,
        })
    })?;
    let points = rows.filter_map(|r| r.ok()).collect();
    Ok(points)
}

fn scoped_metric_series(
    conn: &rusqlite::Connection,
    scope: &str,
    metric_expr: &str,
    bucket_ms: i64,
    from_ms: i64,
) -> Result<Vec<NamedMetricSeries>, rusqlite::Error> {
    let mut target_stmt = conn.prepare(
        "SELECT target_name, COALESCE(MAX(target_name), ''),
                COALESCE(MAX(coverage_status), ''), COALESCE(MAX(coverage_note), '')
         FROM system_metrics
         WHERE ts >= ?1 AND scope = ?2 AND target_name IS NOT NULL AND target_name != ''
         GROUP BY target_name
         ORDER BY MAX(mem_process_mb) DESC, target_name ASC",
    )?;
    let targets = target_stmt
        .query_map(rusqlite::params![from_ms, scope], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
            ))
        })?
        .filter_map(|r| r.ok())
        .collect::<Vec<_>>();

    let sql = format!(
        "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, AVG({metric_expr})
         FROM system_metrics
         WHERE ts >= ?2 AND scope = ?3 AND target_name = ?4
         GROUP BY bucket ORDER BY bucket"
    );

    let mut series = Vec::new();
    for (key, _label, coverage_status, coverage_note) in targets {
        let mut stmt = conn.prepare(&sql)?;
        let points = stmt
            .query_map(
                rusqlite::params![bucket_ms, from_ms, scope, key.as_str()],
                |r| {
                    Ok(MetricPoint {
                        ts: r.get(0)?,
                        value: r.get(1)?,
                    })
                },
            )?
            .filter_map(|r| r.ok())
            .collect::<Vec<_>>();
        if points.is_empty() {
            continue;
        }
        series.push(NamedMetricSeries {
            key: key.clone(),
            label: runtime_label(&key),
            points,
            process_names: vec![runtime_label(&key)],
            coverage_status: if coverage_status.is_empty() {
                None
            } else {
                Some(coverage_status)
            },
            coverage_note: if coverage_note.is_empty() {
                None
            } else {
                Some(coverage_note)
            },
        });
    }
    Ok(series)
}

fn estimated_sidecar_model_mem_series(
    conn: &rusqlite::Connection,
    bucket_ms: i64,
    from_ms: i64,
) -> Result<Vec<NamedMetricSeries>, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT model_type, model_name,
                (ts / ?1) * ?1 + ?1/2 as bucket,
                MAX(COALESCE(memory_mb, 0))
         FROM model_events
         WHERE ts >= ?2
           AND model_type IN ('ocr', 'embedding', 'llm')
           AND event_type IN ('load_start', 'load_done', 'unload')
         GROUP BY model_type, model_name, bucket
         ORDER BY bucket",
    )?;
    let mut grouped: std::collections::BTreeMap<String, (String, Vec<MetricPoint>)> =
        std::collections::BTreeMap::new();
    let rows = stmt.query_map(rusqlite::params![bucket_ms, from_ms], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, i64>(2)?,
            r.get::<_, i64>(3)?,
        ))
    })?;
    for row in rows.filter_map(|r| r.ok()) {
        let key = format!("sidecar_{}", row.0);
        let label = sidecar_model_type_label(&row.0, &row.1);
        grouped
            .entry(key)
            .and_modify(|(_, points)| {
                points.push(MetricPoint {
                    ts: row.2,
                    value: row.3 as f64,
                })
            })
            .or_insert_with(|| {
                (
                    label,
                    vec![MetricPoint {
                        ts: row.2,
                        value: row.3 as f64,
                    }],
                )
            });
    }

    Ok(grouped
        .into_iter()
        .map(|(key, (label, points))| NamedMetricSeries {
            key,
            label,
            points,
            process_names: Vec::new(),
            coverage_status: Some("estimated".to_string()),
            coverage_note: Some(
                "基于 sidecar 模型事件估算的逻辑内存时间线，非进程级精确 RSS".to_string(),
            ),
        })
        .collect())
}

fn latest_system_metrics(
    conn: &rusqlite::Connection,
    has_gpu_percent: bool,
    has_gpu_name: bool,
    scoped: bool,
) -> Option<SystemLatestMetrics> {
    let sql = if scoped {
        "SELECT cpu_total, mem_total_mb, mem_used_mb, mem_percent, gpu_percent, gpu_name
         FROM system_metrics
         WHERE scope = ?1
         ORDER BY ts DESC LIMIT 1"
    } else {
        "SELECT cpu_total, mem_total_mb, mem_used_mb, mem_percent, gpu_percent, gpu_name
         FROM system_metrics
         ORDER BY ts DESC LIMIT 1"
    };

    let result = if scoped {
        conn.query_row(sql, rusqlite::params![SYSTEM_SCOPE], |r| {
            Ok((
                r.get::<_, f64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, i64>(2)?,
                r.get::<_, f64>(3)?,
                if has_gpu_percent {
                    r.get::<_, Option<f64>>(4)?
                } else {
                    None
                },
                if has_gpu_name {
                    r.get::<_, Option<String>>(5)?
                } else {
                    None
                },
            ))
        })
    } else {
        conn.query_row(sql, [], |r| {
            Ok((
                r.get::<_, f64>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, i64>(2)?,
                r.get::<_, f64>(3)?,
                if has_gpu_percent {
                    r.get::<_, Option<f64>>(4)?
                } else {
                    None
                },
                if has_gpu_name {
                    r.get::<_, Option<String>>(5)?
                } else {
                    None
                },
            ))
        })
    };

    result.ok().map(|row| SystemLatestMetrics {
        cpu_total: row.0,
        mem_total_mb: row.1,
        mem_used_mb: row.2,
        mem_percent: row.3,
        gpu_percent: row.4,
        gpu_total_label: row.5.clone(),
        gpu_name: row.5,
    })
}

fn latest_scoped_metrics(conn: &rusqlite::Connection, scope: &str) -> Option<ScopedLatestMetrics> {
    conn.query_row(
        "SELECT cpu_process, mem_process_mb,
                COALESCE(target_pids_json, '[]'),
                coverage_status,
                coverage_note
         FROM system_metrics
         WHERE scope = ?1
         ORDER BY ts DESC LIMIT 1",
        rusqlite::params![scope],
        |r| {
            let pids_json: String = r.get(2)?;
            let process_count = serde_json::from_str::<Vec<i64>>(&pids_json)
                .map(|pids| pids.len())
                .unwrap_or(0);
            Ok(ScopedLatestMetrics {
                cpu_percent: r.get(0)?,
                mem_process_mb: r.get(1)?,
                process_count,
                process_names: if scope == MODEL_SCOPE {
                    let mut names = Vec::new();
                    let mut seen = std::collections::HashSet::new();
                    let mut stmt = conn.prepare(
                        "SELECT target_name FROM system_metrics
                         WHERE scope = ?1 AND ts >= ?2 AND target_name IS NOT NULL AND target_name != ''
                         ORDER BY ts DESC"
                    )?;
                    let rows = stmt.query_map(
                        rusqlite::params![MODEL_SERIES_SCOPE, Utc::now().timestamp_millis() - 24 * 3600 * 1000],
                        |row| row.get::<_, String>(0),
                    )?;
                    for row in rows.filter_map(|row| row.ok()) {
                        let label = runtime_label(&row);
                        if seen.insert(label.clone()) {
                            names.push(label);
                        }
                    }
                    names
                } else {
                    Vec::new()
                },
                coverage_status: r.get(3)?,
                coverage_note: r.get(4)?,
            })
        },
    ).ok()
}

fn latest_model_runtime_breakdown(
    conn: &rusqlite::Connection,
) -> Result<Vec<ModelRuntimeBreakdownItem>, rusqlite::Error> {
    let mut items = Vec::new();

    let mut runtime_stmt = conn.prepare(
        "WITH latest_ts AS (
            SELECT MAX(ts) AS ts
            FROM system_metrics
            WHERE scope = ?1
         )
         SELECT target_name,
                COALESCE(MAX(coverage_status), ''),
                COALESCE(MAX(coverage_note), ''),
                AVG(cpu_process),
                AVG(mem_process_mb),
                COALESCE(target_pids_json, '[]')
         FROM system_metrics, latest_ts
         WHERE scope = ?1 AND system_metrics.ts = latest_ts.ts
         GROUP BY target_name, target_pids_json
         ORDER BY AVG(mem_process_mb) DESC, target_name ASC",
    )?;
    let runtime_rows = runtime_stmt.query_map(rusqlite::params![MODEL_SERIES_SCOPE], |r| {
        let key: String = r.get(0)?;
        let pids_json: String = r.get(5)?;
        let process_count = serde_json::from_str::<Vec<i64>>(&pids_json)
            .map(|pids| pids.len())
            .unwrap_or(0);
        Ok(ModelRuntimeBreakdownItem {
            key: key.clone(),
            label: runtime_label(&key),
            coverage_status: match r.get::<_, String>(1)? {
                value if value.is_empty() => None,
                value => Some(value),
            },
            coverage_note: match r.get::<_, String>(2)? {
                value if value.is_empty() => None,
                value => Some(value),
            },
            cpu_percent: r.get::<_, f64>(3)?,
            mem_process_mb: r.get::<_, f64>(4)?.round() as i64,
            process_count,
        })
    })?;
    items.extend(runtime_rows.filter_map(|r| r.ok()));

    let mut sidecar_stmt = conn.prepare(
        "SELECT model_type, model_name, ts, memory_mb
         FROM model_events
         WHERE ts >= ?1
           AND model_type IN ('ocr', 'embedding', 'llm')
           AND event_type IN ('load_start', 'load_done', 'unload')
         ORDER BY ts DESC",
    )?;
    let cutoff = Utc::now().timestamp_millis() - 24 * 3600 * 1000;
    let mut seen = std::collections::HashSet::new();
    let sidecar_rows = sidecar_stmt.query_map(rusqlite::params![cutoff], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, i64>(2)?,
            r.get::<_, Option<i64>>(3)?.unwrap_or(0),
        ))
    })?;
    for row in sidecar_rows.filter_map(|r| r.ok()) {
        let model_type = row.0;
        let model_name = row.1;
        if !seen.insert(model_type.clone()) {
            continue;
        }
        let key = format!("sidecar_{}", model_type);
        items.push(ModelRuntimeBreakdownItem {
            key,
            label: sidecar_model_type_label(&model_type, &model_name),
            cpu_percent: 0.0,
            mem_process_mb: row.3,
            process_count: 1,
            coverage_status: Some("estimated".to_string()),
            coverage_note: Some(
                "基于 sidecar 模型事件的逻辑拆分，内存为近似值，非进程级精确 RSS".to_string(),
            ),
        });
    }

    items.sort_by(|a, b| b.mem_process_mb.cmp(&a.mem_process_mb));
    Ok(items)
}

/// GET /api/monitor/system?range=1h|6h|24h|1d
pub async fn monitor_system(
    State(state): State<Arc<AppState>>,
    Query(params): Query<SystemQuery>,
) -> Result<impl IntoResponse, ApiError> {
    let now_ms = Utc::now().timestamp_millis();
    let range_ms: i64 = match params.range.as_str() {
        "1h" => 3600 * 1000,
        "6h" => 6 * 3600 * 1000,
        "24h" | "1d" => 24 * 3600 * 1000,
        _ => 24 * 3600 * 1000,
    };
    let from_ms = now_ms - range_ms;
    let fallback_noise_pattern = format!("{}%", FALLBACK_NOISE_OVERVIEW_PREFIX);
    let bucket_ms = system_bucket_ms(&params.range);
    let max_trend_points = match params.range.as_str() {
        "1h" => 120,
        "6h" => 160,
        "24h" | "1d" => 240,
        _ => 160,
    };

    let result = state
        .storage
        .with_conn_async(move |conn| {
            let db_size_bytes = conn
                .query_row("PRAGMA page_count", [], |r| r.get::<_, i64>(0))
                .unwrap_or(0)
                * conn
                    .query_row("PRAGMA page_size", [], |r| r.get::<_, i64>(0))
                    .unwrap_or(0);
            let metric_columns: HashSet<String> = {
                let mut stmt = conn.prepare("PRAGMA table_info(system_metrics)")?;
                let rows = stmt.query_map([], |r| r.get::<_, String>(1))?;
                rows.filter_map(|r| r.ok()).collect()
            };
            let has_gpu_percent = has_column(&metric_columns, "gpu_percent");
            let has_gpu_name = has_column(&metric_columns, "gpu_name");
            let scoped = has_scope_columns(&metric_columns);

            let system_cpu = if scoped {
                scoped_metric_trend(conn, SYSTEM_SCOPE, "cpu_total", bucket_ms, from_ms)?
            } else {
                let mut stmt = conn.prepare(
                    "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, AVG(cpu_total)
                 FROM system_metrics WHERE ts >= ?2
                 GROUP BY bucket ORDER BY bucket",
                )?;
                let rows = stmt.query_map(rusqlite::params![bucket_ms, from_ms], |r| {
                    Ok(MetricPoint {
                        ts: r.get(0)?,
                        value: r.get(1)?,
                    })
                })?;
                rows.filter_map(|r| r.ok()).collect()
            };

            let system_mem = if scoped {
                scoped_metric_trend(conn, SYSTEM_SCOPE, "mem_percent", bucket_ms, from_ms)?
            } else {
                let mut stmt = conn.prepare(
                    "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, AVG(mem_percent)
                 FROM system_metrics WHERE ts >= ?2
                 GROUP BY bucket ORDER BY bucket",
                )?;
                let rows = stmt.query_map(rusqlite::params![bucket_ms, from_ms], |r| {
                    Ok(MetricPoint {
                        ts: r.get(0)?,
                        value: r.get(1)?,
                    })
                })?;
                rows.filter_map(|r| r.ok()).collect()
            };

            let suite_cpu = if scoped {
                scoped_metric_trend(conn, SUITE_SCOPE, "cpu_process", bucket_ms, from_ms)?
            } else {
                Vec::new()
            };
            let suite_mem = if scoped {
                scoped_metric_trend(conn, SUITE_SCOPE, "mem_process_mb", bucket_ms, from_ms)?
            } else {
                Vec::new()
            };
            let model_cpu = if scoped {
                scoped_metric_trend(conn, MODEL_SCOPE, "cpu_process", bucket_ms, from_ms)?
            } else {
                Vec::new()
            };
            let model_mem = if scoped {
                scoped_metric_trend(conn, MODEL_SCOPE, "mem_process_mb", bucket_ms, from_ms)?
            } else {
                Vec::new()
            };
            let model_cpu_series = if scoped {
                scoped_metric_series(conn, MODEL_SERIES_SCOPE, "cpu_process", bucket_ms, from_ms)?
            } else {
                Vec::new()
            };
            let model_mem_series = if scoped {
                scoped_metric_series(
                    conn,
                    MODEL_SERIES_SCOPE,
                    "mem_process_mb",
                    bucket_ms,
                    from_ms,
                )?
            } else {
                Vec::new()
            };
            let model_estimated_mem_series = if scoped {
                estimated_sidecar_model_mem_series(conn, bucket_ms, from_ms)?
            } else {
                Vec::new()
            };

            let gpu_trend: Vec<MetricPoint> = if has_gpu_percent {
                if scoped {
                    let mut stmt = conn.prepare(
                        "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, AVG(gpu_percent)
                     FROM system_metrics
                     WHERE ts >= ?2 AND scope = ?3 AND gpu_percent IS NOT NULL
                     GROUP BY bucket ORDER BY bucket",
                    )?;
                    let rows =
                        stmt.query_map(rusqlite::params![bucket_ms, from_ms, SYSTEM_SCOPE], |r| {
                            Ok(MetricPoint {
                                ts: r.get(0)?,
                                value: r.get(1)?,
                            })
                        })?;
                    rows.filter_map(|r| r.ok()).collect()
                } else {
                    let mut stmt = conn.prepare(
                        "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, AVG(gpu_percent)
                     FROM system_metrics
                     WHERE ts >= ?2 AND gpu_percent IS NOT NULL
                     GROUP BY bucket ORDER BY bucket",
                    )?;
                    let rows = stmt.query_map(rusqlite::params![bucket_ms, from_ms], |r| {
                        Ok(MetricPoint {
                            ts: r.get(0)?,
                            value: r.get(1)?,
                        })
                    })?;
                    rows.filter_map(|r| r.ok()).collect()
                }
            } else {
                Vec::new()
            };
            let model_gpu_trend: Vec<MetricPoint> = if has_gpu_percent && scoped {
                let mut stmt = conn.prepare(
                    "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, AVG(gpu_percent)
                 FROM system_metrics
                 WHERE ts >= ?2 AND scope = ?3 AND gpu_percent IS NOT NULL
                 GROUP BY bucket ORDER BY bucket",
                )?;
                let rows =
                    stmt.query_map(rusqlite::params![bucket_ms, from_ms, MODEL_SCOPE], |r| {
                        Ok(MetricPoint {
                            ts: r.get(0)?,
                            value: r.get(1)?,
                        })
                    })?;
                rows.filter_map(|r| r.ok()).collect()
            } else {
                Vec::new()
            };

            let disk_trend: Vec<DiskPoint> = if scoped {
                let mut stmt = conn.prepare(
                    "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, SUM(disk_read_mb), SUM(disk_write_mb)
                 FROM system_metrics
                 WHERE ts >= ?2 AND scope = ?3
                 GROUP BY bucket ORDER BY bucket",
                )?;
                let rows =
                    stmt.query_map(rusqlite::params![bucket_ms, from_ms, SYSTEM_SCOPE], |r| {
                        Ok(DiskPoint {
                            ts: r.get(0)?,
                            read_mb: r.get(1)?,
                            write_mb: r.get(2)?,
                        })
                    })?;
                rows.filter_map(|r| r.ok()).collect()
            } else {
                let mut stmt = conn.prepare(
                    "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, SUM(disk_read_mb), SUM(disk_write_mb)
                 FROM system_metrics
                 WHERE ts >= ?2
                 GROUP BY bucket ORDER BY bucket",
                )?;
                let rows = stmt.query_map(rusqlite::params![bucket_ms, from_ms], |r| {
                    Ok(DiskPoint {
                        ts: r.get(0)?,
                        read_mb: r.get(1)?,
                        write_mb: r.get(2)?,
                    })
                })?;
                rows.filter_map(|r| r.ok()).collect()
            };

            let latest = ResourceLatestBundle {
                system: latest_system_metrics(conn, has_gpu_percent, has_gpu_name, scoped),
                suite: if scoped {
                    latest_scoped_metrics(conn, SUITE_SCOPE)
                } else {
                    None
                },
                model: if scoped {
                    latest_scoped_metrics(conn, MODEL_SCOPE)
                } else {
                    None
                },
            };
            let model_runtime_breakdown = if scoped {
                latest_model_runtime_breakdown(conn)?
            } else {
                Vec::new()
            };

            let mut knowledge_events_stmt = conn.prepare(
                "SELECT (ts / ?1) * ?1 + ?1/2 as bucket, COUNT(*)
             FROM (
                SELECT CAST(strftime('%s', created_at) AS INTEGER) * 1000 as ts
                FROM timelines
                WHERE created_at >= datetime(?2/1000, 'unixepoch')
                  AND summary NOT LIKE ?3
             )
             GROUP BY bucket ORDER BY bucket",
            )?;
            let knowledge_events: Vec<KnowledgeTimePoint> = knowledge_events_stmt
                .query_map(
                    rusqlite::params![bucket_ms, from_ms, fallback_noise_pattern.as_str()],
                    |r| {
                        Ok(KnowledgeTimePoint {
                            ts: r.get(0)?,
                            count: r.get(1)?,
                        })
                    },
                )?
                .filter_map(|r| r.ok())
                .collect();

            let mut ev_stmt = conn.prepare(
                "SELECT ts, event_type, model_type, model_name,
                    duration_ms, memory_mb, mem_before_mb, mem_after_mb, error_msg
             FROM model_events WHERE ts >= ?1
             ORDER BY ts DESC LIMIT 50",
            )?;
            let model_events: Vec<ModelEventItem> = ev_stmt
                .query_map(rusqlite::params![from_ms], |r| {
                    let model_type = r.get::<_, String>(2)?;
                    let model_name = r.get::<_, String>(3)?;
                    Ok(ModelEventItem {
                        ts: r.get(0)?,
                        event_type: r.get(1)?,
                        model_type: model_type.clone(),
                        model_name: public_model_event_name(&model_type, &model_name),
                        duration_ms: r.get(4)?,
                        memory_mb: r.get(5)?,
                        mem_before_mb: r.get(6)?,
                        mem_after_mb: r.get(7)?,
                        error_msg: r.get(8)?,
                    })
                })?
                .filter_map(|r| r.ok())
                .collect();

            let system_cpu = downsample_metric_points(system_cpu, max_trend_points);
            let system_mem = downsample_metric_points(system_mem, max_trend_points);
            let suite_cpu = downsample_metric_points(suite_cpu, max_trend_points);
            let suite_mem = downsample_metric_points(suite_mem, max_trend_points);
            let model_cpu = downsample_metric_points(model_cpu, max_trend_points);
            let model_mem = downsample_metric_points(model_mem, max_trend_points);
            let model_cpu_series =
                downsample_named_metric_series(model_cpu_series, max_trend_points);
            let model_mem_series =
                downsample_named_metric_series(model_mem_series, max_trend_points);
            let model_estimated_mem_series =
                downsample_named_metric_series(model_estimated_mem_series, max_trend_points);
            let gpu_trend = downsample_metric_points(gpu_trend, max_trend_points);
            let model_gpu_trend = downsample_metric_points(model_gpu_trend, max_trend_points);
            let disk_trend = downsample_disk_points(disk_trend, max_trend_points);
            let knowledge_events = downsample_knowledge_points(knowledge_events, max_trend_points);

            Ok(SystemResourcesResponse {
                db_size_bytes,
                trends: ResourceTrends {
                    system_cpu,
                    system_mem,
                    suite_cpu,
                    suite_mem,
                    model_cpu,
                    model_mem,
                    model_cpu_series,
                    model_mem_series,
                    model_estimated_mem_series,
                },
                gpu_trend,
                model_gpu_trend,
                disk_trend,
                knowledge_events,
                model_events,
                model_runtime_breakdown,
                latest,
            })
        })
        .await?;

    Ok(Json(result))
}

// ── 提炼流水线 DAG 端点 ──────────────────────────────────────────────────────
//
// GET /api/monitor/pipeline_dag
// 用于「监控 → DAG」子页面 3s 轮询。
// 阶段定义：
//   capture   → 采集（pending = 已采集但还没生成 timeline 的 capture）
//   timeline  → 预提炼（pending = 已 timeline 但下游 bake_* 都为空）
//   knowledge / sop / document / data → 自动入库产物，不再维护人工确认队列
// in-progress：
//   capture：sidecar status.json 中正在提炼的 capture（来自 enrich_extractor_status）
//   timeline / knowledge / sop / document / data：当前 running bake run 的活跃批次数，并复用
//                                                 待处理 timeline 作为详情占位，避免总数和抽屉列表脱节。

const DAG_ITEM_LIMIT: i64 = 20;
const DAG_TIMELINE_PENDING_WINDOW_MS: i64 = 7 * 24 * 3600 * 1000;
const DAG_RUNNING_BAKE_STALE_MS: i64 = 35 * 60 * 1000;

#[derive(Debug, Serialize)]
pub struct PipelineDagResponse {
    pub server_now_ms: i64,
    pub extractor_status: String,
    pub capture_enabled: bool,
    /// 兼容旧 UI：第一个 running bake run（如果有）
    pub running_bake_run: Option<DagRunningRun>,
    /// 所有正在运行的 bake run 列表
    pub running_bake_runs: Vec<DagRunningRun>,
    /// bake 流水线的 unified watermark 距离最老一条等待处理候选的 ms 间隔。
    /// 用来揭穿"timeline pending=0 但其实 watermark 卡死"的假象：
    /// 如果 lag_ms 远大于正常推进节奏，说明系统假装空闲实际堆积。0 表示已追上。
    pub bake_watermark_lag_ms: i64,
    pub stages: Vec<DagStage>,
}

#[derive(Debug, Serialize, Clone)]
pub struct DagRunningRun {
    pub id: i64,
    pub trigger_reason: String,
    pub started_at: i64,
    pub candidate_count: i64,
    pub processed_episode_count: i64,
}

#[derive(Debug, Serialize)]
pub struct DagStage {
    pub key: String,
    pub label: String,
    pub in_progress_label: String,
    pub pending_label: String,
    pub in_progress_count: i64,
    pub pending_count: i64,
    pub completed_today: i64,
    pub in_progress_items: Vec<DagItem>,
    pub pending_items: Vec<DagItem>,
}

#[derive(Debug, Serialize, Clone)]
pub struct DagItem {
    pub kind: String,
    pub id: i64,
    pub ts: i64,
    pub title: String,
    pub subtitle: Option<String>,
    pub started_at_ms: Option<i64>,
}

pub async fn monitor_pipeline_dag(
    State(state): State<Arc<AppState>>,
) -> Result<impl IntoResponse, ApiError> {
    let now_ms = Utc::now().timestamp_millis();
    let capture_enabled = state.is_capture_enabled();
    let day_start_ms = local_day_start_ms(now_ms);
    // bake_knowledge/bake_sops 的 created_at 是文本格式 "YYYY-MM-DD HH:MM:SS"，用字符串前缀比较今日
    let day_start_str = chrono::DateTime::<Utc>::from_timestamp_millis(day_start_ms)
        .map(|dt| dt.with_timezone(&Local).format("%Y-%m-%d").to_string())
        .unwrap_or_else(|| Local::now().format("%Y-%m-%d").to_string());
    let pending_from_ms = now_ms - DAG_TIMELINE_PENDING_WINDOW_MS;
    let bake_queue_status = state
        .storage
        .get_bake_queue_status(MAX_BAKE_RETRY_FAILURES)
        .unwrap_or_default();

    // 1. 实时提炼状态（sidecar）
    let mut tmp_flow = KnowledgeFlow {
        today_count: 0,
        period_count: 0,
        capture_enabled,
        pending_extraction_count: 0,
        oldest_pending_extraction_at_ms: None,
        extraction_stalled: false,
        pending_bake_count: 0,
        oldest_pending_bake_at_ms: None,
        bake_drain_rate_per_hour: 0.0,
        bake_eta_ms: None,
        bake_retry_pending_count: 0,
        bake_fresh_pending_count: 0,
        bake_retry_ready_count: 0,
        bake_retry_delayed_count: 0,
        bake_actionable_count: 0,
        bake_retry_exhausted_count: 0,
        bake_timeout_failure_count: 0,
        bake_truncated_failure_count: 0,
        bake_other_failure_count: 0,
        bake_upstream_failure_count: 0,
        bake_recent_no_progress_count: 0,
        bake_next_retry_at_ms: None,
        bake_recommended_retry_after_ms: 0,
        bake_scheduler_mismatch: false,
        running_bake_count: 0,
        stale_bake_run_count: 0,
        by_time: Vec::new(),
        recent: Vec::new(),
        extracting: Vec::new(),
        last_extraction_at_ms: None,
        extractor_status: "stalled".to_string(),
    };
    enrich_extractor_status(&mut tmp_flow, now_ms).await;
    let extracting_captures = tmp_flow.extracting;
    let extractor_status = tmp_flow.extractor_status;

    // 2. SQL 聚合：把 6 个 stage 的数字 + 每个 stage 的 pending 列表一次性查出来
    let extracting_ids: Vec<i64> = extracting_captures.iter().map(|c| c.id).collect();

    let aggregated = state
        .storage
        .with_conn_async(move |conn| -> Result<DagAggregated, crate::storage::error::StorageError> {
            // 今日产量统计需要按文档成员精确匹配 episode；早期 LIKE '%'||id||'%'
            // 对每条 timeline 全扫 bake_documents 子串，5000×700 次要接近 1 秒。
            refresh_doc_member_temp_tables(conn)?;
            // ── capture ────────────────────────────────────────────────────
            let placeholders = if extracting_ids.is_empty() {
                String::new()
            } else {
                format!(
                    " AND id NOT IN ({})",
                    extracting_ids
                        .iter()
                        .map(|id| id.to_string())
                        .collect::<Vec<_>>()
                        .join(",")
                )
            };
            let capture_app_not_like =
                build_not_like_clause("app_name", &SELF_GENERATED_APP_KEYWORDS);
            let capture_win_not_like =
                build_not_like_clause("win_title", &SELF_GENERATED_WINDOW_KEYWORDS);
            let capture_items_sql = format!(
                "SELECT id, ts, COALESCE(app_name, ''), COALESCE(win_title, '')
                 FROM captures
                 WHERE timeline_id IS NULL
                   AND is_sensitive = 0
                   AND (COALESCE(ax_text, '') != ''
                        OR COALESCE(ocr_text, '') != ''
                        OR COALESCE(input_text, '') != ''
                        OR COALESCE(audio_text, '') != '')
                   AND ({capture_app_not_like})
                   AND ({capture_win_not_like}){placeholders}
                 ORDER BY ts ASC
                 LIMIT ?1"
            );
            let mut stmt = conn.prepare(&capture_items_sql)?;
            let capture_pending_items: Vec<DagItem> = stmt
                .query_map(rusqlite::params![DAG_ITEM_LIMIT], |r| {
                    let id: i64 = r.get(0)?;
                    let ts: i64 = r.get(1)?;
                    let app: String = r.get(2)?;
                    let title: String = r.get(3)?;
                    Ok(DagItem {
                        kind: "capture".to_string(),
                        id,
                        ts,
                        title: if title.is_empty() { app.clone() } else { title },
                        subtitle: if app.is_empty() { None } else { Some(app) },
                        started_at_ms: None,
                    })
                })?
                .filter_map(|r| r.ok())
                .collect();
            drop(stmt);

            let capture_completed_today: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM captures c
                     JOIN timelines t ON t.id = c.timeline_id
                     WHERE c.timeline_id IS NOT NULL
                       AND COALESCE(t.created_at_ms, CAST(strftime('%s', t.created_at) AS INTEGER) * 1000) >= ?1",
                    rusqlite::params![day_start_ms],
                    |r| r.get(0),
                )
                .unwrap_or(0);

            // ── timeline（预提炼）─────────────────────────────────────────
            // pending = 水位线之后、已 timeline 但下游 bake_knowledge / bake_sops / bake_documents 都未产出
            // 同时对齐 bake 流水线的候选条件：
            //   1. 排除 bake_article / bake_knowledge / bake_sop 分类（自生成条目不进 bake）
            //   2. 排除因重试失败次数 >= 3 而被永久跳过的条目
            //   3. 排除不满足 is_high_value_candidate 条件的低价值条目
            //      （importance < 4 且 evidence_strength NOT IN ('high','medium')）
            //   4. 排除 unified bake 水位线之前已经处理过但被 LLM 判定丢弃的条目
            // 产物排除改为查询级 CTE：逐条 timeline 关联 json_each 子查询单次就要
            // 数秒，3 秒轮询会把共享数据库连接占满，监控页因此打开缓慢。
            let timeline_pending_sql = format!(
                "WITH {produced_doc_timelines_cte}
                SELECT t.id,
                       MAX(
                           COALESCE(t.updated_at_ms, 0),
                           COALESCE((SELECT MAX(c2.ts) FROM captures c2 WHERE c2.timeline_id = t.id), 0)
                       ) AS ts_ms,
                       COALESCE(t.summary, ''),
                       COALESCE(t.frag_app_name, ''),
                       COALESCE(t.frag_win_title, '')
                FROM timelines t
                LEFT JOIN bake_retry_state r ON r.timeline_id = t.id
                WHERE t.category NOT IN ('bake_article', 'bake_knowledge', 'bake_sop', 'legacy_bake_candidate')
                  AND t.is_self_generated = 0
                  AND COALESCE(r.failure_count, 0) < 3
                  AND (
                      t.importance >= 4
                      OR t.user_verified = 1
                      OR t.history_view = 1
                      OR (
                          t.evidence_strength IN ('high', 'medium')
                          AND (t.activity_type IN ('coding','reading','reviewing_history','document_reference')
                               OR t.content_origin IN ('historical_content','live_interaction')
                          )
                      )
                      OR EXISTS (
                          SELECT 1 FROM captures dc
                          WHERE dc.timeline_id = t.id
                            AND (
                                 LOWER(COALESCE(dc.url, '')) LIKE '%/docs/%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%docs.google%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%/document/%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%yuque.com%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%feishu.cn/docx%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%feishu.cn/wiki%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%notion.so%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%confluence%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%/wiki/%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%shimo.im%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%/d/home/%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%/s/home/%'
                              OR LOWER(COALESCE(dc.url, '')) LIKE '%/k/home/%'
                            )
                            AND LENGTH(
                                  REPLACE(
                                      REPLACE(
                                          COALESCE(dc.ax_text, '') || COALESCE(dc.ocr_text, ''),
                                          ' ',
                                          ''
                                      ),
                                      char(10),
                                      ''
                                  )
                                ) >= 200
                      )
                  )
                  AND NOT EXISTS (SELECT 1 FROM bake_knowledge bk WHERE bk.timeline_id = t.id)
                  AND NOT EXISTS (SELECT 1 FROM bake_sops bs WHERE bs.timeline_id = t.id)
                  AND CAST(t.id AS TEXT) NOT IN (SELECT tid FROM produced_doc_timelines)
                  AND (
                    MAX(
                        COALESCE(t.updated_at_ms, 0),
                        COALESCE(
                            (SELECT MAX(c4.ts) FROM captures c4 WHERE c4.timeline_id = t.id),
                            0
                        )
                    ) > COALESCE(
                            (
                                SELECT MAX(last_processed_ts)
                                FROM bake_watermarks
                                WHERE pipeline_name = 'unified'
                            ),
                            0
                          )
                    OR COALESCE(r.failure_count, 0) > 0
                  )
                ORDER BY ts_ms ASC
                LIMIT ?1",
                produced_doc_timelines_cte = PRODUCED_DOC_TIMELINES_CTE
            );
            let mut stmt = conn.prepare(&timeline_pending_sql)?;
            let timeline_pending_items: Vec<DagItem> = stmt
                .query_map(rusqlite::params![DAG_ITEM_LIMIT], |r| {
                    let id: i64 = r.get(0)?;
                    let ts: i64 = r.get(1)?;
                    let summary: String = r.get(2)?;
                    let app: String = r.get(3)?;
                    let win: String = r.get(4)?;
                    let subtitle = match (app.is_empty(), win.is_empty()) {
                        (true, true) => None,
                        (false, true) => Some(app),
                        (true, false) => Some(win),
                        (false, false) => Some(format!("{} · {}", app, win)),
                    };
                    Ok(DagItem {
                        kind: "timeline".to_string(),
                        id,
                        ts,
                        title: if summary.is_empty() {
                            format!("Timeline #{}", id)
                        } else {
                            summary
                        },
                        subtitle,
                        started_at_ms: None,
                    })
                })?
                .filter_map(|r| r.ok())
                .collect();
            drop(stmt);

            // 按下游产出表的 created_at 过滤今日，反映真实的今日 bake 产量。
            // 注意 bake_knowledge/bake_sops 的 created_at 是文本 'YYYY-MM-DD HH:MM:SS'，
            // 用 date() 归一化后比较；bake_documents 是毫秒 INTEGER，需 /1000 转 unixepoch。
            // 文档成员匹配走 doc_member_episode 临时表（精确 JSON 成员），避免
            // LIKE 子串双重循环；同时也修正了旧子串匹配可能误计 id 前缀重叠的问题。
            let timeline_completed_today: i64 = conn
                .query_row(
                    "SELECT COUNT(DISTINCT t.id)
                     FROM timelines t
                     WHERE (
                         EXISTS (
                             SELECT 1 FROM bake_knowledge bk
                             WHERE bk.timeline_id = t.id
                               AND date(bk.created_at) >= date(?1)
                         )
                         OR EXISTS (
                             SELECT 1 FROM bake_sops bs
                             WHERE bs.timeline_id = t.id
                               AND date(bs.created_at) >= date(?1)
                         )
                         OR EXISTS (
                             SELECT 1 FROM doc_member_episode de
                             WHERE de.episode_id = CAST(t.id AS TEXT)
                               AND date(de.doc_created_at / 1000, 'unixepoch') >= date(?1)
                         )
                     )",
                    rusqlite::params![day_start_str],
                    |r| r.get(0),
                )
                .unwrap_or(0);

            // 产物“今日完成”统一读取持久化生产事件：自动提炼按 bake run 的
            // 完成计数，手工产物按首次创建补入。这样合并到既有资产仍会计入，
            // 普通 updated_at 维护刷新则不会误报。
            let today_production = load_bake_production_events(conn, day_start_ms)?;
            let knowledge_completed_today = today_production
                .iter()
                .map(|event| event.knowledge_count)
                .sum();
            let document_completed_today = today_production
                .iter()
                .map(|event| event.document_count)
                .sum();
            let sop_completed_today = today_production
                .iter()
                .map(|event| event.sop_count)
                .sum();

            // ── knowledge ─────────────────────────────────────────────────
            let _ = load_candidate_stage(
                conn,
                "bake_knowledge",
                pending_from_ms,
                day_start_ms,
            )?;

            // ── sop ───────────────────────────────────────────────────────
            let _ = load_candidate_stage(
                conn,
                "bake_sop",
                pending_from_ms,
                day_start_ms,
            )?;

            // ── data ──────────────────────────────────────────────────────
            // 只统计已经生成快照的数据项；仅识别出报表 URL、尚未采集正文的来源
            // 仍属于待采集来源，不能作为已完成的数据产物。
            let data_completed_today = load_data_completed_today(conn, day_start_ms)?;

            // ── 当前 running 的 bake_run（全部，支持并发显示）─────────────────
            let fresh_running_after_ms = now_ms - DAG_RUNNING_BAKE_STALE_MS;
            let mut running_stmt = conn.prepare(
                "SELECT id, trigger_reason, started_at,
                        COALESCE(candidate_count, 0),
                        COALESCE(processed_episode_count, 0)
                 FROM bake_runs
                 WHERE status = 'running'
                   AND started_at >= ?1
                 ORDER BY started_at DESC",
            )?;
            let running_runs: Vec<DagRunningRun> = running_stmt
                .query_map(rusqlite::params![fresh_running_after_ms], |r| {
                    Ok(DagRunningRun {
                        id: r.get(0)?,
                        trigger_reason: r.get(1)?,
                        started_at: r.get(2)?,
                        candidate_count: r.get(3)?,
                        processed_episode_count: r.get(4)?,
                    })
                })?
                .filter_map(|r| r.ok())
                .collect();
            drop(running_stmt);

            // 首页与 DAG 使用同一份库存口径。上面的列表查询只返回若干条
            // 用于抽屉展示；节点数字来自与实际 bake 候选一致的全量统计。
            let backlog = load_pipeline_backlog_metrics(
                conn,
                now_ms,
                capture_enabled,
                &bake_queue_status,
            )
            .unwrap_or_default();
            let capture_pending_count = backlog.pending_extraction_count;
            let timeline_pending_count = backlog.pending_bake_count;
            let bake_watermark_lag_ms = backlog
                .oldest_pending_bake_at_ms
                .map(|oldest| now_ms.saturating_sub(oldest).max(0))
                .unwrap_or(0);

            Ok(DagAggregated {
                capture_pending_count,
                capture_pending_items,
                capture_completed_today,
                timeline_pending_count,
                timeline_pending_items,
                timeline_completed_today,
                knowledge_pending_count: 0,
                knowledge_pending_items: Vec::new(),
                knowledge_completed_today,
                sop_pending_count: 0,
                sop_pending_items: Vec::new(),
                sop_completed_today,
                document_pending_count: 0,
                document_pending_items: Vec::new(),
                document_completed_today,
                data_pending_count: 0,
                data_pending_items: Vec::new(),
                data_completed_today,
                running_runs,
                bake_watermark_lag_ms,
            })
        })
        .await?;

    // 3. 拼装 capture 阶段的 in-progress（来自 sidecar）
    let capture_in_progress_items: Vec<DagItem> = extracting_captures
        .iter()
        .map(|c| DagItem {
            kind: "capture".to_string(),
            id: c.id,
            ts: c.ts,
            title: if c.win_title.is_empty() {
                c.app_name.clone()
            } else {
                c.win_title.clone()
            },
            subtitle: if c.app_name.is_empty() {
                None
            } else {
                Some(c.app_name.clone())
            },
            started_at_ms: Some(c.group_started_at_ms),
        })
        .collect();
    let capture_in_progress_count = capture_in_progress_items.len() as i64;

    // bake run 的 candidate_count 是整批候选总数，不能直接当作"正在提炼"。
    // 这里用运行中的 run 数作为活跃 LLM 调用占位，并用 remaining 兜底避免空跑时显示。
    let bake_remaining: i64 = aggregated
        .running_runs
        .iter()
        .map(|r| r.candidate_count.saturating_sub(r.processed_episode_count))
        .sum();
    let bake_active_runs = aggregated
        .running_runs
        .iter()
        .filter(|r| r.candidate_count == 0 || r.candidate_count > r.processed_episode_count)
        .count() as i64;
    let bake_in_progress = bake_active_runs.min(bake_remaining.max(bake_active_runs));
    let bake_started_at = aggregated.running_runs.first().map(|r| r.started_at);
    let timeline_in_progress_items: Vec<DagItem> = aggregated
        .timeline_pending_items
        .iter()
        .take(bake_in_progress.max(0) as usize)
        .cloned()
        .map(|mut item| {
            item.started_at_ms = bake_started_at;
            item
        })
        .collect();

    let stages = vec![
        DagStage {
            key: "capture".to_string(),
            label: "采集".to_string(),
            in_progress_label: "提炼中".to_string(),
            pending_label: "排队".to_string(),
            in_progress_count: capture_in_progress_count,
            pending_count: aggregated.capture_pending_count,
            completed_today: aggregated.capture_completed_today,
            in_progress_items: capture_in_progress_items,
            pending_items: aggregated.capture_pending_items,
        },
        DagStage {
            key: "timeline".to_string(),
            label: "预提炼".to_string(),
            in_progress_label: "提炼中".to_string(),
            pending_label: "待提炼".to_string(),
            in_progress_count: bake_in_progress,
            pending_count: aggregated.timeline_pending_count,
            completed_today: aggregated.timeline_completed_today,
            in_progress_items: timeline_in_progress_items,
            pending_items: aggregated.timeline_pending_items,
        },
        DagStage {
            key: "knowledge".to_string(),
            label: "知识".to_string(),
            in_progress_label: "生成中".to_string(),
            pending_label: "".to_string(),
            in_progress_count: 0,
            pending_count: aggregated.knowledge_pending_count,
            completed_today: aggregated.knowledge_completed_today,
            in_progress_items: Vec::new(),
            pending_items: aggregated.knowledge_pending_items,
        },
        DagStage {
            key: "sop".to_string(),
            label: "操作手册".to_string(),
            in_progress_label: "生成中".to_string(),
            pending_label: "".to_string(),
            in_progress_count: 0,
            pending_count: aggregated.sop_pending_count,
            completed_today: aggregated.sop_completed_today,
            in_progress_items: Vec::new(),
            pending_items: aggregated.sop_pending_items,
        },
        DagStage {
            key: "document".to_string(),
            label: "文档".to_string(),
            in_progress_label: "生成中".to_string(),
            pending_label: "".to_string(),
            in_progress_count: 0,
            pending_count: aggregated.document_pending_count,
            completed_today: aggregated.document_completed_today,
            in_progress_items: Vec::new(),
            pending_items: aggregated.document_pending_items,
        },
        DagStage {
            key: "data".to_string(),
            label: "数据".to_string(),
            in_progress_label: "生成中".to_string(),
            pending_label: "".to_string(),
            in_progress_count: 0,
            pending_count: aggregated.data_pending_count,
            completed_today: aggregated.data_completed_today,
            in_progress_items: Vec::new(),
            pending_items: aggregated.data_pending_items,
        },
    ];

    let first_run = aggregated.running_runs.first().cloned();

    Ok(Json(PipelineDagResponse {
        server_now_ms: now_ms,
        extractor_status,
        capture_enabled,
        running_bake_run: first_run,
        running_bake_runs: aggregated.running_runs,
        bake_watermark_lag_ms: aggregated.bake_watermark_lag_ms,
        stages,
    }))
}

struct DagAggregated {
    capture_pending_count: i64,
    capture_pending_items: Vec<DagItem>,
    capture_completed_today: i64,
    timeline_pending_count: i64,
    timeline_pending_items: Vec<DagItem>,
    timeline_completed_today: i64,
    knowledge_pending_count: i64,
    knowledge_pending_items: Vec<DagItem>,
    knowledge_completed_today: i64,
    sop_pending_count: i64,
    sop_pending_items: Vec<DagItem>,
    sop_completed_today: i64,
    document_pending_count: i64,
    document_pending_items: Vec<DagItem>,
    document_completed_today: i64,
    data_pending_count: i64,
    data_pending_items: Vec<DagItem>,
    data_completed_today: i64,
    running_runs: Vec<DagRunningRun>,
    /// 兼容字段：最老一条待烘焙 timeline 的实际等待时长（ms），0 表示无库存。
    bake_watermark_lag_ms: i64,
}

fn load_data_completed_today(
    conn: &rusqlite::Connection,
    day_start_ms: i64,
) -> Result<i64, crate::storage::error::StorageError> {
    conn.query_row(
        "SELECT COUNT(DISTINCT source.id)
         FROM data_sources source
         JOIN data_snapshots snapshot ON snapshot.source_id = source.id
         WHERE source.deleted_at IS NULL
           AND source.status != 'disabled'
           AND snapshot.created_at >= ?1",
        rusqlite::params![day_start_ms],
        |row| row.get(0),
    )
    .map_err(Into::into)
}

// stage 取值：'bake_knowledge' → 表 bake_knowledge / kind 'bake_knowledge'
//             'bake_sop'       → 表 bake_sops      / kind 'bake_sop'
fn load_candidate_stage(
    conn: &rusqlite::Connection,
    stage: &str,
    _pending_from_ms: i64,
    day_start_ms: i64,
) -> Result<(i64, Vec<DagItem>, i64), crate::storage::error::StorageError> {
    let (table, kind) = match stage {
        "bake_knowledge" => ("bake_knowledge", "bake_knowledge"),
        "bake_sop" => ("bake_sops", "bake_sop"),
        other => {
            tracing::warn!("load_candidate_stage: 未知 stage {}", other);
            return Ok((0, Vec::new(), 0));
        }
    };

    // 时间字段：created_at_ms（毫秒）若为空则把 created_at（ISO8601 文本）转换
    let ts_expr = "COALESCE(created_at_ms, CAST(strftime('%s', created_at) AS INTEGER) * 1000)";

    let pending_count: i64 = conn
        .query_row(
            &format!("SELECT COUNT(*) FROM {table} WHERE user_verified = 0"),
            [],
            |r| r.get(0),
        )
        .unwrap_or(0);

    let pending_sql = format!(
        "SELECT id, {ts_expr}, COALESCE(title, ''), COALESCE(summary, '')
         FROM {table}
         WHERE user_verified = 0
         ORDER BY {ts_expr} DESC
         LIMIT ?1"
    );
    let mut stmt = conn.prepare(&pending_sql)?;
    let kind_owned = kind.to_string();
    let pending_items: Vec<DagItem> = stmt
        .query_map(rusqlite::params![DAG_ITEM_LIMIT], |r| {
            let id: i64 = r.get(0)?;
            let ts: i64 = r.get(1).unwrap_or(0);
            let title: String = r.get(2)?;
            let summary: String = r.get(3)?;
            Ok(DagItem {
                kind: kind_owned.clone(),
                id,
                ts,
                title: if title.is_empty() {
                    format!("#{}", id)
                } else {
                    title
                },
                subtitle: if summary.is_empty() {
                    None
                } else {
                    Some(summary)
                },
                started_at_ms: None,
            })
        })?
        .filter_map(|r| r.ok())
        .collect();
    drop(stmt);

    // 今日完成与记忆页「记忆生产历程」同口径：按 created_at（今日新建）统计，
    // 并排除总览页不再展示的旧版自动产物。
    // 不能用 updated_at_ms —— 后台任务会批量刷新历史条目的更新时间，
    // 会把大量历史记录误算成今日完成；也不按 user_verified 过滤，
    // 产物落库即算完成，与 pending（待确认）指标解耦。
    let completed_today: i64 = conn
        .query_row(
            &format!(
                "SELECT COUNT(*) FROM {table}
                 WHERE {ts_expr} >= ?1
                   AND NOT (
                       COALESCE(
                           json_extract(
                               CASE WHEN json_valid(content) THEN content ELSE '{{}}' END,
                               '$.creation_mode'
                           ),
                           ''
                       ) = 'auto'
                       AND COALESCE(
                           json_extract(
                               CASE WHEN json_valid(content) THEN content ELSE '{{}}' END,
                               '$.generation_version'
                           ),
                           ''
                       ) = ?2
                   )"
            ),
            rusqlite::params![day_start_ms, BAKE_GENERATION_VERSION],
            |r| r.get(0),
        )
        .unwrap_or(0);

    Ok((pending_count, pending_items, completed_today))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn monitor_backlog_excludes_memory_bread_preview_window() {
        let clause = build_not_like_clause("c.win_title", &SELF_GENERATED_WINDOW_KEYWORDS);
        assert!(clause.contains("%memorybread preview%"));
        assert!(!SELF_GENERATED_WINDOW_KEYWORDS.contains(&"memorybread"));
    }

    #[test]
    fn data_stage_counts_only_non_deleted_snapshot_sources_from_today() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE data_sources (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                deleted_at INTEGER
             );
             CREATE TABLE data_snapshots (
                id INTEGER PRIMARY KEY,
                source_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
             );
             INSERT INTO data_sources VALUES
                (1, 'active', NULL),
                (2, 'active', NULL),
                (3, 'disabled', NULL),
                (4, 'active', 2500),
                (5, 'unavailable', NULL);
             INSERT INTO data_snapshots VALUES
                (1, 1, 2500),
                (2, 3, 2600),
                (3, 4, 2700),
                (4, 5, 2800);",
        )
        .unwrap();

        assert_eq!(load_data_completed_today(&conn, 2000).unwrap(), 2);
        assert_eq!(load_data_completed_today(&conn, 3000).unwrap(), 0);
    }

    #[test]
    fn candidate_stage_completed_today_counts_created_not_updated() {
        // 今日完成必须按 created_at 统计：历史条目被后台任务刷新 updated_at_ms
        // 不能被算进今日完成，与记忆页「记忆生产历程」口径保持一致。
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE bake_knowledge (
                id INTEGER PRIMARY KEY,
                title TEXT,
                summary TEXT,
                content TEXT,
                user_verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER
             );",
        )
        .unwrap();
        // 今日新建（应计入）
        conn.execute(
            "INSERT INTO bake_knowledge (id, title, summary, user_verified, created_at, created_at_ms, updated_at_ms)
             VALUES (1, '今日新建', '', 1, '2026-08-07 09:00:00', 3000, 3100)",
            [],
        )
        .unwrap();
        // 历史创建、今日被刷新 updated_at_ms（不应计入）
        conn.execute(
            "INSERT INTO bake_knowledge (id, title, summary, user_verified, created_at, created_at_ms, updated_at_ms)
             VALUES (2, '历史被刷新', '', 1, '2026-08-06 09:00:00', 2000, 3200)",
            [],
        )
        .unwrap();
        // 今日新建但未确认（同样计入完成，与 pending 指标解耦）
        conn.execute(
            "INSERT INTO bake_knowledge (id, title, summary, user_verified, created_at, created_at_ms, updated_at_ms)
             VALUES (3, '今日未确认', '', 0, '2026-08-07 10:00:00', 3300, 3300)",
            [],
        )
        .unwrap();
        // 今日旧版自动产物（总览页不展示，监控也不应多算）
        conn.execute(
            "INSERT INTO bake_knowledge (
                 id, title, summary, content, user_verified,
                 created_at, created_at_ms, updated_at_ms
             ) VALUES (
                 4, '旧版自动产物', '',
                 '{\"creation_mode\":\"auto\",\"generation_version\":\"bake-v1\"}',
                 1, '2026-08-07 11:00:00', 3400, 3400
             )",
            [],
        )
        .unwrap();

        let (pending, _, completed_today) =
            load_candidate_stage(&conn, "bake_knowledge", 0, 2500).unwrap();
        assert_eq!(pending, 1);
        assert_eq!(completed_today, 2);
    }

    #[test]
    fn bake_stall_requires_old_backlog_and_no_recent_progress() {
        let now_ms = 10_000_000;
        let old = now_ms - BAKE_WATERMARK_STALL_MS - 1;
        let recent = now_ms - BAKE_WATERMARK_STALL_MS + 1;

        assert!(is_bake_pipeline_stalled(
            true,
            now_ms,
            12,
            Some(old),
            Some(old),
        ));
        assert!(!is_bake_pipeline_stalled(
            true,
            now_ms,
            12,
            Some(old),
            Some(recent),
        ));
        assert!(!is_bake_pipeline_stalled(
            true,
            now_ms,
            0,
            Some(old),
            Some(old),
        ));
        assert!(!is_bake_pipeline_stalled(
            false,
            now_ms,
            12,
            Some(old),
            Some(old),
        ));
    }

    #[test]
    fn stale_heartbeat_does_not_report_healthy_services_as_down() {
        assert_eq!(
            service_health_status(true, true, true, true, true, true),
            "degraded",
        );
        assert_eq!(
            service_health_status(false, true, true, true, true, false),
            "ok",
        );
        assert_eq!(
            service_health_status(false, false, true, true, true, true),
            "down",
        );
    }

    #[test]
    fn bake_token_distribution_reports_quantiles_and_separate_buckets() {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE llm_usage_logs (
                ts INTEGER NOT NULL,
                caller TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                status TEXT,
                done_reason TEXT
             );
             INSERT INTO llm_usage_logs VALUES
                (1000, 'bake', 3500, 400, 'success', 'stop'),
                (2000, 'bake', 21000, 2500, 'success', 'stop'),
                (3000, 'bake', 6500, 26190, 'success', 'length'),
                (4000, 'bake', 10, 8, 'success', 'stop'),
                (5000, 'rag', 25000, 9000, 'success', 'stop');",
        )
        .unwrap();

        let distribution = load_bake_token_distribution(&conn, 0).unwrap();

        assert_eq!(distribution.sample_count, 3);
        assert_eq!(distribution.truncated_count, 1);
        assert_eq!(distribution.input_over_20k_count, 1);
        assert_eq!(distribution.input.p50, 6500);
        assert_eq!(distribution.input.max, 21000);
        assert_eq!(distribution.output.p50, 2500);
        assert_eq!(distribution.output.max, 26190);
        assert_eq!(
            distribution
                .output
                .buckets
                .iter()
                .find(|bucket| bucket.label == "≥20K")
                .map(|bucket| bucket.count),
            Some(1),
        );
    }

    #[test]
    fn provider_model_names_are_branded_before_monitor_serialization() {
        assert_eq!(public_llm_model_name("provider-local-model"), "MBEM v1.0");
        assert_eq!(
            public_llm_model_name("provider-plus-model"),
            "MBCD Plus v1.0"
        );
        assert_eq!(
            public_model_event_name("embedding", "provider-vector-model"),
            "MBEMB V1.0"
        );
    }
}
