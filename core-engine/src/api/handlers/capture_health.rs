//! GET /api/monitor/capture-health — 采集可靠性与空窗监控。

use std::sync::Arc;

use axum::{extract::State, Json};
use serde::Serialize;

use crate::api::{error::ApiError, state::AppState};
use crate::storage::db::current_ts_ms;

const HEALTH_RANGE_MS: i64 = 6 * 60 * 60 * 1000;
const MAX_ACTIVE_BLACKOUT_MS: i64 = 120 * 1000;

#[derive(Debug, Serialize)]
pub struct CaptureAttemptCount {
    pub outcome: String,
    pub reason: String,
    pub count: i64,
}

#[derive(Debug, Serialize)]
pub struct RecentCaptureAttempt {
    pub observed_at: i64,
    pub event_type: String,
    pub outcome: String,
    pub reason: String,
    pub capture_id: Option<i64>,
    pub related_capture_id: Option<i64>,
    pub app_name: Option<String>,
    pub win_title: Option<String>,
    pub is_private: bool,
    pub effective_interval_secs: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct CaptureHealthResponse {
    pub capture_enabled: bool,
    pub configured_interval_secs: u64,
    pub effective_interval_secs: u64,
    pub pressure_degraded: bool,
    pub last_attempt_at_ms: Option<i64>,
    pub last_captured_at_ms: Option<i64>,
    pub last_continuity_at_ms: Option<i64>,
    pub content_blackout_ms: Option<i64>,
    pub max_active_blackout_ms: i64,
    pub outcome_counts: Vec<CaptureAttemptCount>,
    pub recent: Vec<RecentCaptureAttempt>,
    pub server_now_ms: i64,
}

pub async fn monitor_capture_health(
    State(state): State<Arc<AppState>>,
) -> Result<Json<CaptureHealthResponse>, ApiError> {
    let now_ms = current_ts_ms();
    let from_ms = now_ms - HEALTH_RANGE_MS;
    let storage = state.storage.clone();
    let (last_attempt_at_ms, last_captured_at_ms, last_continuity_at_ms, outcome_counts, recent) =
        tokio::task::spawn_blocking(move || {
            storage.with_conn(|conn| {
                let last_attempt_at_ms =
                    conn.query_row("SELECT MAX(observed_at) FROM capture_attempts", [], |row| {
                        row.get::<_, Option<i64>>(0)
                    })?;
                let last_captured_at_ms = conn.query_row(
                    "SELECT MAX(observed_at) FROM capture_attempts WHERE outcome = 'captured'",
                    [],
                    |row| row.get::<_, Option<i64>>(0),
                )?;
                let last_continuity_at_ms = conn.query_row(
                    "SELECT MAX(observed_at) FROM capture_attempts
                     WHERE outcome IN ('continuity', 'deduplicated')",
                    [],
                    |row| row.get::<_, Option<i64>>(0),
                )?;

                let mut count_stmt = conn.prepare(
                    "SELECT outcome, reason, COUNT(*)
                     FROM capture_attempts
                     WHERE observed_at >= ?1
                     GROUP BY outcome, reason
                     ORDER BY COUNT(*) DESC, outcome, reason",
                )?;
                let outcome_counts = count_stmt
                    .query_map(rusqlite::params![from_ms], |row| {
                        Ok(CaptureAttemptCount {
                            outcome: row.get(0)?,
                            reason: row.get(1)?,
                            count: row.get(2)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;
                drop(count_stmt);

                let mut recent_stmt = conn.prepare(
                    "SELECT observed_at, event_type, outcome, reason,
                            capture_id, related_capture_id, app_name, win_title,
                            is_private, effective_interval_secs
                     FROM capture_attempts
                     ORDER BY observed_at DESC, id DESC
                     LIMIT 20",
                )?;
                let recent = recent_stmt
                    .query_map([], |row| {
                        Ok(RecentCaptureAttempt {
                            observed_at: row.get(0)?,
                            event_type: row.get(1)?,
                            outcome: row.get(2)?,
                            reason: row.get(3)?,
                            capture_id: row.get(4)?,
                            related_capture_id: row.get(5)?,
                            app_name: row.get(6)?,
                            win_title: row.get(7)?,
                            is_private: row.get::<_, i64>(8)? != 0,
                            effective_interval_secs: row.get(9)?,
                        })
                    })?
                    .collect::<Result<Vec<_>, _>>()?;

                Ok((
                    last_attempt_at_ms,
                    last_captured_at_ms,
                    last_continuity_at_ms,
                    outcome_counts,
                    recent,
                ))
            })
        })
        .await
        .map_err(|error| ApiError::Internal(error.to_string()))??;

    let last_content_evidence_at_ms = match (last_captured_at_ms, last_continuity_at_ms) {
        (Some(captured), Some(continuity)) => Some(captured.max(continuity)),
        (captured, continuity) => captured.or(continuity),
    };

    Ok(Json(CaptureHealthResponse {
        capture_enabled: state.is_capture_enabled(),
        configured_interval_secs: state.capture_schedule.configured_interval_secs(),
        effective_interval_secs: state.capture_schedule.effective_interval_secs(),
        pressure_degraded: state.capture_schedule.pressure_degraded(),
        last_attempt_at_ms,
        last_captured_at_ms,
        last_continuity_at_ms,
        content_blackout_ms: last_content_evidence_at_ms.map(|value| now_ms.saturating_sub(value)),
        max_active_blackout_ms: MAX_ACTIVE_BLACKOUT_MS,
        outcome_counts,
        recent,
        server_now_ms: now_ms,
    }))
}
