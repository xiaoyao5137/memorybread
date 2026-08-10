//! GET /api/work-profile - 返回个人工作画像所需的本地聚合统计。

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use axum::{
    extract::{Query, State},
    Json,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::api::{error::ApiError, state::AppState};
use crate::storage::models::{CaptureActivityAggregate, WorkCategoryTotals, WorkImCaptureSample};

const DAY_MS: i64 = 86_400_000;
const MAX_RANGE_DAYS: i64 = 400;
const IDLE_GAP_CAP_MS: i64 = 5 * 60 * 1000;
const LAST_CAPTURE_TAIL_MS: i64 = 60 * 1000;
const OVERNIGHT_END_HOUR: i64 = 6;
// 深度专注：同一应用连续有效工作不切换的单段最小长度。
const MIN_FOCUS_RUN_MS: i64 = 45 * 60 * 1000;
// 采集记录无法直接证明用户已经入睡，因此以 4 小时以上的连续空档作为候选睡眠段；
// 同一自然日存在多个候选时，后续只采用最长的一段作为当天作息分界。
const MIN_SLEEP_GAP_MS: i64 = 4 * 60 * 60 * 1000;
const WORKDAY_CONTEXT_MS: i64 = 2 * DAY_MS;
const MAX_IM_CAPTURE_SAMPLES: usize = 200;

#[derive(Debug, Deserialize)]
pub struct WorkProfileQuery {
    pub from: i64,
    pub to: i64,
    #[serde(default)]
    pub timezone_offset_minutes: i32,
    #[serde(default)]
    pub include_achievement_metrics: bool,
    #[serde(default)]
    pub include_day_details: bool,
}

#[derive(Debug, Serialize)]
pub struct WorkProfileResponse {
    pub range_start: i64,
    pub range_end: i64,
    pub idle_gap_cap_minutes: i64,
    pub total_minutes: i64,
    pub active_days: usize,
    pub current_streak: usize,
    pub longest_streak: usize,
    pub longest_day_minutes: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub achievement_metrics: Option<AchievementMetrics>,
    pub today: TodayWorkSummary,
    pub days: Vec<WorkDaySummary>,
}

/// 标签卡片只消费本地计算后的时长峰值与分类时长，不包含任何工作内容。
///
/// 分类时长字段在旧核心进程上可能缺失，客户端必须按可选字段处理。
#[derive(Debug, Serialize)]
pub struct AchievementMetrics {
    pub longest_work_session_minutes: i64,
    pub max_overnight_work_minutes: i64,
    pub interruption_gap_minutes: i64,
    pub overnight_start_hour: i64,
    pub overnight_end_hour: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub coding_minutes: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub design_minutes: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub focus_minutes: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub knowledge_minutes: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct TodayWorkSummary {
    pub date: String,
    pub total_minutes: i64,
    pub capture_count: i64,
    pub active_period_count: i64,
    pub first_capture_at: Option<i64>,
    pub last_capture_at: Option<i64>,
    pub apps: Vec<WorkAppSummary>,
    pub mood: TodayMoodSummary,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkMood {
    Energized,
    Focused,
    Steady,
    Tired,
    Overloaded,
}

#[derive(Debug, Serialize)]
pub struct TodayMoodSummary {
    pub inferred: bool,
    pub mood: Option<WorkMood>,
    pub expression_count: usize,
    pub source_apps: Vec<String>,
}

#[derive(Debug, Serialize)]
pub struct WorkAppSummary {
    pub name: String,
    pub minutes: i64,
    pub capture_count: i64,
}

#[derive(Debug, Serialize)]
pub struct WorkDaySummary {
    pub date: String,
    pub minutes: i64,
    pub capture_count: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub active_period_count: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub first_capture_at: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_capture_at: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub apps: Option<Vec<WorkAppSummary>>,
}

#[derive(Debug, Default)]
struct DayAccumulator {
    duration_ms: i64,
    capture_count: i64,
    active_period_count: i64,
    first_ts: Option<i64>,
    last_ts: Option<i64>,
    apps: Vec<WorkAppSummary>,
}

pub async fn get_work_profile(
    State(state): State<Arc<AppState>>,
    Query(params): Query<WorkProfileQuery>,
) -> Result<Json<WorkProfileResponse>, ApiError> {
    validate_query(&params)?;

    let timezone_offset_ms = i64::from(params.timezone_offset_minutes) * 60_000;
    let range_start = params.from;
    let range_end = params.to;
    let include_achievement_metrics = params.include_achievement_metrics;
    let (today_start, today_end) = current_local_day_range(timezone_offset_ms)?;
    let mood_start = range_start.max(today_start);
    let mood_end = range_end.min(today_end);
    let activity_start = range_start.saturating_sub(IDLE_GAP_CAP_MS);
    let activity_end = range_end.saturating_add(IDLE_GAP_CAP_MS);
    let includes_today = range_start < today_end && range_end > today_start;
    let workday_context = if params.include_day_details {
        Some((
            range_start.saturating_sub(WORKDAY_CONTEXT_MS),
            range_end.saturating_add(WORKDAY_CONTEXT_MS),
        ))
    } else if includes_today {
        Some((
            today_start.saturating_sub(WORKDAY_CONTEXT_MS),
            today_end.saturating_add(WORKDAY_CONTEXT_MS),
        ))
    } else {
        None
    };
    let timestamp_range = match (workday_context, include_achievement_metrics) {
        (Some((workday_start, workday_end)), true) => Some((
            workday_start.min(activity_start),
            workday_end.max(activity_end),
        )),
        (Some(range), false) => Some(range),
        (None, true) => Some((activity_start, activity_end)),
        (None, false) => None,
    };
    let storage = state.storage.clone();
    let (rows, im_samples, timestamps, category_totals) = tokio::task::spawn_blocking(move || {
        let rows = storage.summarize_capture_activity(
            range_start,
            range_end,
            timezone_offset_ms,
            IDLE_GAP_CAP_MS,
        )?;
        let im_samples = if mood_start < mood_end {
            storage.list_enabled_company_im_capture_samples(
                mood_start,
                mood_end,
                MAX_IM_CAPTURE_SAMPLES,
            )?
        } else {
            Vec::new()
        };
        let timestamps = timestamp_range
            .map(|(start, end)| storage.list_capture_activity_timestamps(start, end))
            .transpose()?
            .unwrap_or_default();
        // 分类时长属于增强指标：统计失败时降级为零值，不阻断工作画像主流程。
        let category_totals = if include_achievement_metrics {
            storage
                .summarize_work_category_minutes(
                    range_start,
                    range_end,
                    IDLE_GAP_CAP_MS,
                    MIN_FOCUS_RUN_MS,
                )
                .unwrap_or_default()
        } else {
            Default::default()
        };
        Ok::<_, crate::storage::error::StorageError>((
            rows,
            im_samples,
            timestamps,
            category_totals,
        ))
    })
    .await
    .map_err(|error| ApiError::Internal(error.to_string()))??;

    let mood = infer_work_mood(&im_samples);
    let response = build_response(
        rows,
        mood,
        &timestamps,
        include_achievement_metrics,
        category_totals,
        range_start,
        range_end,
        timezone_offset_ms,
        params.include_day_details,
    )?;
    Ok(Json(response))
}

fn validate_query(params: &WorkProfileQuery) -> Result<(), ApiError> {
    let range_ms = params
        .to
        .checked_sub(params.from)
        .filter(|range| *range > 0)
        .ok_or_else(|| ApiError::BadRequest("to must be greater than from".to_string()))?;
    if range_ms > MAX_RANGE_DAYS * DAY_MS {
        return Err(ApiError::BadRequest(format!(
            "work profile range must not exceed {MAX_RANGE_DAYS} days"
        )));
    }
    if !(-720..=840).contains(&params.timezone_offset_minutes) {
        return Err(ApiError::BadRequest(
            "timezone_offset_minutes is out of range".to_string(),
        ));
    }
    Ok(())
}

fn build_response(
    rows: Vec<CaptureActivityAggregate>,
    mood: TodayMoodSummary,
    timestamps: &[i64],
    include_achievement_metrics: bool,
    category_totals: WorkCategoryTotals,
    range_start: i64,
    range_end: i64,
    timezone_offset_ms: i64,
    include_day_details: bool,
) -> Result<WorkProfileResponse, ApiError> {
    let inferred_workdays = infer_workday_time_ranges(timestamps, timezone_offset_ms);
    let mut days: BTreeMap<i64, DayAccumulator> = BTreeMap::new();
    for row in rows {
        let day = days.entry(row.day_index).or_default();
        day.duration_ms += row.duration_ms;
        day.capture_count += row.capture_count;
        day.active_period_count += row.active_period_count;
        day.first_ts = Some(
            day.first_ts
                .map_or(row.first_ts, |value| value.min(row.first_ts)),
        );
        day.last_ts = Some(
            day.last_ts
                .map_or(row.last_ts, |value| value.max(row.last_ts)),
        );
        day.apps.push(WorkAppSummary {
            name: row.app_name,
            minutes: round_minutes(row.duration_ms),
            capture_count: row.capture_count,
        });
    }

    let now_day_index = (Utc::now().timestamp_millis() + timezone_offset_ms) / DAY_MS;
    let today_date = day_index_to_date(now_day_index)?;
    let today_accumulator = days.get(&now_day_index);
    let today_time_range = inferred_workdays.get(&now_day_index);
    let today = TodayWorkSummary {
        date: today_date,
        total_minutes: today_accumulator
            .map(|day| round_minutes(day.duration_ms))
            .unwrap_or_default(),
        capture_count: today_accumulator
            .map(|day| day.capture_count)
            .unwrap_or_default(),
        active_period_count: today_accumulator
            .map(|day| day.active_period_count)
            .unwrap_or_default(),
        first_capture_at: today_time_range
            .map(|range| range.first_ts)
            .or_else(|| today_accumulator.and_then(|day| day.first_ts)),
        last_capture_at: today_time_range
            .map(|range| range.last_ts)
            .or_else(|| today_accumulator.and_then(|day| day.last_ts)),
        apps: compact_apps(
            today_accumulator
                .map(|day| day.apps.as_slice())
                .unwrap_or_default(),
        ),
        mood,
    };

    let active_day_indexes = days.keys().copied().collect::<Vec<_>>();
    let (current_streak, longest_streak) = streaks(&active_day_indexes, now_day_index);
    let day_summaries = days
        .iter()
        .map(|(day_index, day)| {
            let inferred_time_range = inferred_workdays.get(day_index);
            Ok(WorkDaySummary {
                date: day_index_to_date(*day_index)?,
                minutes: round_minutes(day.duration_ms),
                capture_count: day.capture_count,
                active_period_count: include_day_details.then_some(day.active_period_count),
                first_capture_at: include_day_details
                    .then(|| {
                        inferred_time_range
                            .map(|range| range.first_ts)
                            .or(day.first_ts)
                    })
                    .flatten(),
                last_capture_at: include_day_details
                    .then(|| {
                        inferred_time_range
                            .map(|range| range.last_ts)
                            .or(day.last_ts)
                    })
                    .flatten(),
                apps: include_day_details.then(|| compact_apps(&day.apps)),
            })
        })
        .collect::<Result<Vec<_>, ApiError>>()?;
    let total_minutes = days.values().map(|day| day.duration_ms).sum::<i64>();
    let longest_day_minutes = days
        .values()
        .map(|day| round_minutes(day.duration_ms))
        .max()
        .unwrap_or_default();
    let achievement_metrics = include_achievement_metrics.then(|| {
        build_achievement_metrics(
            timestamps,
            range_start,
            range_end,
            timezone_offset_ms,
            category_totals,
        )
    });

    Ok(WorkProfileResponse {
        range_start,
        range_end,
        idle_gap_cap_minutes: IDLE_GAP_CAP_MS / 60_000,
        total_minutes: round_minutes(total_minutes),
        active_days: days.len(),
        current_streak,
        longest_streak,
        longest_day_minutes,
        achievement_metrics,
        today,
        days: day_summaries,
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct WorkdayTimeRange {
    first_ts: i64,
    last_ts: i64,
}

#[derive(Debug, Clone, Copy)]
struct SleepBoundary {
    gap_ms: i64,
    timestamp_index: usize,
}

/// 以“睡眠后的首条记录”作为工作日开始，并让结束时间自然跨过午夜。
///
/// 一天内可能同时出现午休、外出和夜间睡眠等多个长空档。这里按唤醒后的本地日期
/// 只保留最长空档，避免把较短的白天空档误当成新工作日。查询方会额外读取前后两天
/// 的时间戳，因此自然日零点附近的延续工作也能归入正确的作息周期。
fn infer_workday_time_ranges(
    timestamps: &[i64],
    timezone_offset_ms: i64,
) -> BTreeMap<i64, WorkdayTimeRange> {
    let mut ordered = timestamps.to_vec();
    ordered.sort_unstable();
    ordered.dedup();
    let Some(&first_timestamp) = ordered.first() else {
        return BTreeMap::new();
    };

    let first_day_index = first_timestamp
        .saturating_add(timezone_offset_ms)
        .div_euclid(DAY_MS);
    let mut boundaries = BTreeMap::from([(
        first_day_index,
        SleepBoundary {
            gap_ms: 0,
            timestamp_index: 0,
        },
    )]);

    for (index, window) in ordered.windows(2).enumerate() {
        let gap_ms = window[1].saturating_sub(window[0]);
        if gap_ms < MIN_SLEEP_GAP_MS {
            continue;
        }
        let timestamp_index = index + 1;
        let day_index = window[1]
            .saturating_add(timezone_offset_ms)
            .div_euclid(DAY_MS);
        let candidate = SleepBoundary {
            gap_ms,
            timestamp_index,
        };
        boundaries
            .entry(day_index)
            .and_modify(|current| {
                if candidate.gap_ms > current.gap_ms
                    || (candidate.gap_ms == current.gap_ms
                        && candidate.timestamp_index < current.timestamp_index)
                {
                    *current = candidate;
                }
            })
            .or_insert(candidate);
    }

    let mut ordered_boundaries = boundaries.into_iter().collect::<Vec<_>>();
    ordered_boundaries.sort_by_key(|(_, boundary)| boundary.timestamp_index);
    let mut result = BTreeMap::new();
    for (index, (day_index, boundary)) in ordered_boundaries.iter().enumerate() {
        let next_start_index = ordered_boundaries
            .get(index + 1)
            .map(|(_, next)| next.timestamp_index)
            .unwrap_or(ordered.len());
        if boundary.timestamp_index >= next_start_index {
            continue;
        }
        result.insert(
            *day_index,
            WorkdayTimeRange {
                first_ts: ordered[boundary.timestamp_index],
                last_ts: ordered[next_start_index - 1],
            },
        );
    }
    result
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct ActiveSession {
    start: i64,
    end: i64,
}

fn build_achievement_metrics(
    timestamps: &[i64],
    range_start: i64,
    range_end: i64,
    timezone_offset_ms: i64,
    category_totals: WorkCategoryTotals,
) -> AchievementMetrics {
    let sessions = build_active_sessions(timestamps, range_start, range_end);
    let longest_work_session_ms = sessions
        .iter()
        .map(|session| session.end.saturating_sub(session.start))
        .max()
        .unwrap_or_default();
    let max_overnight_work_ms = sessions
        .iter()
        .map(|session| overnight_overlap_ms(*session, timezone_offset_ms))
        .max()
        .unwrap_or_default();

    AchievementMetrics {
        longest_work_session_minutes: floor_minutes(longest_work_session_ms),
        max_overnight_work_minutes: floor_minutes(max_overnight_work_ms),
        interruption_gap_minutes: IDLE_GAP_CAP_MS / 60_000,
        overnight_start_hour: 0,
        overnight_end_hour: OVERNIGHT_END_HOUR,
        coding_minutes: Some(floor_minutes(category_totals.coding_ms)),
        design_minutes: Some(floor_minutes(category_totals.design_ms)),
        focus_minutes: Some(floor_minutes(category_totals.focus_ms)),
        knowledge_minutes: Some(floor_minutes(category_totals.knowledge_ms)),
    }
}

fn build_active_sessions(
    timestamps: &[i64],
    range_start: i64,
    range_end: i64,
) -> Vec<ActiveSession> {
    if range_start >= range_end {
        return Vec::new();
    }

    let mut ordered = timestamps.to_vec();
    ordered.sort_unstable();
    ordered.dedup();
    let Some(&first) = ordered.first() else {
        return Vec::new();
    };

    let mut sessions = Vec::new();
    let mut session_start = first;
    let mut previous = first;
    for &timestamp in ordered.iter().skip(1) {
        if timestamp.saturating_sub(previous) > IDLE_GAP_CAP_MS {
            push_clamped_session(
                &mut sessions,
                session_start,
                previous.saturating_add(IDLE_GAP_CAP_MS),
                range_start,
                range_end,
            );
            session_start = timestamp;
        }
        previous = timestamp;
    }
    push_clamped_session(
        &mut sessions,
        session_start,
        previous.saturating_add(LAST_CAPTURE_TAIL_MS),
        range_start,
        range_end,
    );
    sessions
}

fn push_clamped_session(
    sessions: &mut Vec<ActiveSession>,
    start: i64,
    end: i64,
    range_start: i64,
    range_end: i64,
) {
    let clamped = ActiveSession {
        start: start.max(range_start),
        end: end.min(range_end),
    };
    if clamped.start < clamped.end {
        sessions.push(clamped);
    }
}

fn overnight_overlap_ms(session: ActiveSession, timezone_offset_ms: i64) -> i64 {
    if session.start >= session.end {
        return 0;
    }
    let first_day = session
        .start
        .saturating_add(timezone_offset_ms)
        .div_euclid(DAY_MS);
    let last_day = session
        .end
        .saturating_sub(1)
        .saturating_add(timezone_offset_ms)
        .div_euclid(DAY_MS);
    let mut maximum = 0;
    for day_index in first_day..=last_day {
        let window_start = day_index
            .saturating_mul(DAY_MS)
            .saturating_sub(timezone_offset_ms);
        let window_end = window_start.saturating_add(OVERNIGHT_END_HOUR * 60 * 60 * 1000);
        let overlap_start = session.start.max(window_start);
        let overlap_end = session.end.min(window_end);
        maximum = maximum.max(overlap_end.saturating_sub(overlap_start));
    }
    maximum
}

fn floor_minutes(duration_ms: i64) -> i64 {
    duration_ms.max(0) / 60_000
}

fn compact_apps(apps: &[WorkAppSummary]) -> Vec<WorkAppSummary> {
    let mut sorted = apps
        .iter()
        .map(|app| WorkAppSummary {
            name: app.name.clone(),
            minutes: app.minutes,
            capture_count: app.capture_count,
        })
        .collect::<Vec<_>>();
    sorted.sort_by(|left, right| {
        right
            .minutes
            .cmp(&left.minutes)
            .then_with(|| left.name.cmp(&right.name))
    });
    if sorted.len() <= 5 {
        return sorted;
    }

    let remainder = sorted.split_off(5);
    sorted.push(WorkAppSummary {
        name: "其他".to_string(),
        minutes: remainder.iter().map(|app| app.minutes).sum(),
        capture_count: remainder.iter().map(|app| app.capture_count).sum(),
    });
    sorted
}

fn streaks(active_days: &[i64], today_day_index: i64) -> (usize, usize) {
    let mut longest = 0;
    let mut running = 0;
    let mut previous: Option<i64> = None;

    for day in active_days {
        running = if previous.is_some_and(|value| *day == value + 1) {
            running + 1
        } else {
            1
        };
        longest = longest.max(running);
        previous = Some(*day);
    }

    let current = if active_days.last().copied() == Some(today_day_index) {
        running
    } else {
        0
    };
    (current, longest)
}

fn round_minutes(duration_ms: i64) -> i64 {
    (duration_ms.max(0) + 30_000) / 60_000
}

fn current_local_day_range(timezone_offset_ms: i64) -> Result<(i64, i64), ApiError> {
    let now = Utc::now().timestamp_millis();
    let local_day_index = now
        .checked_add(timezone_offset_ms)
        .ok_or_else(|| ApiError::Internal("invalid local time".to_string()))?
        .div_euclid(DAY_MS);
    let start = local_day_index
        .checked_mul(DAY_MS)
        .and_then(|value| value.checked_sub(timezone_offset_ms))
        .ok_or_else(|| ApiError::Internal("invalid local day range".to_string()))?;
    let end = start
        .checked_add(DAY_MS)
        .ok_or_else(|| ApiError::Internal("invalid local day range".to_string()))?;
    Ok((start, end))
}

fn infer_work_mood(samples: &[WorkImCaptureSample]) -> TodayMoodSummary {
    let source_apps = samples
        .iter()
        .map(|sample| sample.app_name.trim())
        .filter(|app_name| !app_name.is_empty())
        .map(str::to_string)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .take(4)
        .collect::<Vec<_>>();

    if samples.is_empty() {
        return TodayMoodSummary {
            inferred: false,
            mood: None,
            expression_count: 0,
            source_apps,
        };
    }

    const OVERLOADED: &[&str] = &[
        "来不及",
        "赶不及",
        "忙不过来",
        "排不过来",
        "事情太多",
        "任务太多",
        "压力很大",
        "压力好大",
        "要崩",
        "崩溃",
        "焦虑",
        "超负荷",
        "非常紧急",
        "严重延期",
        "完全卡住",
        "顶不住",
        "扛不住",
        "overwhelmed",
        "too much",
        "urgent",
        "blocked",
    ];
    const TIRED: &[&str] = &[
        "有点累",
        "好累",
        "太累",
        "累了",
        "很困",
        "太困",
        "疲惫",
        "没精神",
        "头疼",
        "休息一下",
        "熬夜",
        "加班到",
        "撑不住",
    ];
    const ENERGIZED: &[&str] = &[
        "搞定了",
        "完成了",
        "顺利完成",
        "太好了",
        "好耶",
        "很开心",
        "很期待",
        "进展不错",
        "效果不错",
        "没问题",
        "可以的",
        "冲一把",
        "感谢",
        "谢谢",
        "辛苦了",
        "great",
        "awesome",
        "nice",
        "done",
    ];
    const FOCUSED: &[&str] = &[
        "我来处理",
        "我来跟进",
        "正在处理",
        "正在排查",
        "正在推进",
        "我先确认",
        "我会确认",
        "马上处理",
        "稍后同步",
        "今天完成",
        "今天提交",
        "计划完成",
        "继续推进",
        "安排一下",
        "跟进一下",
        "排查一下",
        "整理一下",
        "review",
        "debug",
        "fix",
    ];

    let mut overloaded_score = 0;
    let mut tired_score = 0;
    let mut energized_score = 0;
    let mut focused_score = 0;
    for sample in samples {
        let text = sample.text.to_lowercase();
        overloaded_score += keyword_matches(&text, OVERLOADED) * 4;
        tired_score += keyword_matches(&text, TIRED) * 4;
        energized_score += keyword_matches(&text, ENERGIZED) * 2;
        focused_score += keyword_matches(&text, FOCUSED);
    }

    let highest = overloaded_score
        .max(tired_score)
        .max(energized_score)
        .max(focused_score);
    let mood = if highest == 0 {
        WorkMood::Steady
    } else if overloaded_score == highest {
        WorkMood::Overloaded
    } else if tired_score == highest {
        WorkMood::Tired
    } else if energized_score == highest {
        WorkMood::Energized
    } else {
        WorkMood::Focused
    };

    TodayMoodSummary {
        inferred: true,
        mood: Some(mood),
        expression_count: samples.len(),
        source_apps,
    }
}

fn keyword_matches(text: &str, keywords: &[&str]) -> i32 {
    keywords
        .iter()
        .filter(|keyword| text.contains(**keyword))
        .count() as i32
}

fn day_index_to_date(day_index: i64) -> Result<String, ApiError> {
    DateTime::<Utc>::from_timestamp_millis(day_index * DAY_MS)
        .map(|date| date.format("%Y-%m-%d").to_string())
        .ok_or_else(|| ApiError::Internal("invalid work profile date".to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn activity(day_index: i64, app_name: &str, minutes: i64) -> CaptureActivityAggregate {
        let first_ts = day_index * DAY_MS + 9 * 60 * 60 * 1000;
        CaptureActivityAggregate {
            day_index,
            app_name: app_name.to_string(),
            duration_ms: minutes * 60_000,
            capture_count: 2,
            active_period_count: 1,
            first_ts,
            last_ts: first_ts + minutes * 60_000,
        }
    }

    fn sample(app_name: &str, text: &str) -> WorkImCaptureSample {
        WorkImCaptureSample {
            app_name: app_name.to_string(),
            text: text.to_string(),
        }
    }

    #[test]
    fn builds_today_totals_and_app_distribution() {
        let today = Utc::now().timestamp_millis() / DAY_MS;
        let response = build_response(
            vec![activity(today, "Code", 7), activity(today, "Browser", 1)],
            infer_work_mood(&[]),
            &[],
            false,
            WorkCategoryTotals::default(),
            (today - 1) * DAY_MS,
            (today + 1) * DAY_MS,
            0,
            true,
        )
        .unwrap();

        assert_eq!(response.total_minutes, 8);
        assert_eq!(response.active_days, 1);
        assert_eq!(response.today.total_minutes, 8);
        assert_eq!(response.today.capture_count, 4);
        assert_eq!(response.today.active_period_count, 2);
        assert_eq!(response.today.apps[0].name, "Code");
        assert_eq!(response.today.apps[0].minutes, 7);
        assert_eq!(
            response.days[0].first_capture_at,
            response.today.first_capture_at
        );
        assert_eq!(
            response.days[0].last_capture_at,
            response.today.last_capture_at
        );
        let day_apps = response.days[0].apps.as_ref().unwrap();
        assert_eq!(day_apps[0].name, "Code");
        assert_eq!(day_apps[0].minutes, 7);
        assert_eq!(response.current_streak, 1);
        assert!(!response.today.mood.inferred);
    }

    #[test]
    fn keeps_annual_day_summaries_lightweight_by_default() {
        let today = Utc::now().timestamp_millis() / DAY_MS;
        let response = build_response(
            vec![activity(today - 1, "Code", 45)],
            infer_work_mood(&[]),
            &[],
            false,
            WorkCategoryTotals::default(),
            (today - 2) * DAY_MS,
            today * DAY_MS,
            0,
            false,
        )
        .unwrap();

        assert_eq!(response.days[0].minutes, 45);
        assert!(response.days[0].first_capture_at.is_none());
        assert!(response.days[0].last_capture_at.is_none());
        assert!(response.days[0].active_period_count.is_none());
        assert!(response.days[0].apps.is_none());
    }

    #[test]
    fn infers_first_capture_after_sleep_and_allows_next_day_end() {
        let day = 20_000 * DAY_MS;
        let hours = |value: i64| value * 60 * 60 * 1000;
        let minutes = |value: i64| value * 60 * 1000;
        let timestamps = vec![
            day - hours(2),
            day,
            day + hours(2),
            day + hours(9),
            day + hours(12),
            day + hours(17),
            day + hours(23) + minutes(30),
            day + DAY_MS + hours(1) + minutes(15),
            day + DAY_MS + hours(9),
        ];

        let ranges = infer_workday_time_ranges(&timestamps, 0);
        let workday = ranges.get(&20_000).unwrap();

        assert_eq!(workday.first_ts, day + hours(9));
        assert_eq!(workday.last_ts, day + DAY_MS + hours(1) + minutes(15));

        let response = build_response(
            vec![activity(20_000, "Code", 60)],
            infer_work_mood(&[]),
            &timestamps,
            false,
            WorkCategoryTotals::default(),
            day,
            day + DAY_MS,
            0,
            true,
        )
        .unwrap();
        assert_eq!(response.days[0].first_capture_at, Some(day + hours(9)));
        assert_eq!(
            response.days[0].last_capture_at,
            Some(day + DAY_MS + hours(1) + minutes(15))
        );
    }

    #[test]
    fn longest_daily_gap_wins_over_shorter_daytime_break() {
        let day = 20_000 * DAY_MS;
        let hours = |value: i64| value * 60 * 60 * 1000;
        let timestamps = vec![
            day - hours(2),
            day + hours(2),
            day + hours(9),
            day + hours(12),
            day + hours(17),
            day + hours(22),
            day + DAY_MS + hours(9),
        ];

        let ranges = infer_workday_time_ranges(&timestamps, 0);
        let workday = ranges.get(&20_000).unwrap();

        assert_eq!(workday.first_ts, day + hours(9));
        assert_eq!(workday.last_ts, day + hours(22));
    }

    #[test]
    fn measures_full_overnight_session_in_local_time() {
        let timezone_offset_ms = 8 * 60 * 60 * 1000;
        let local_midnight = 20_000 * DAY_MS - timezone_offset_ms;
        let timestamps = (0..=73)
            .map(|index| local_midnight - 60_000 + index * 5 * 60_000)
            .collect::<Vec<_>>();

        let metrics = build_achievement_metrics(
            &timestamps,
            local_midnight,
            local_midnight + DAY_MS,
            timezone_offset_ms,
            WorkCategoryTotals::default(),
        );

        assert_eq!(metrics.max_overnight_work_minutes, 360);
        assert!(metrics.longest_work_session_minutes >= 360);
    }

    #[test]
    fn exposes_category_minutes_when_achievement_metrics_are_requested() {
        let day = 20_000 * DAY_MS;
        let totals = WorkCategoryTotals {
            coding_ms: 90 * 60_000 + 29_000,
            design_ms: 30 * 60_000,
            knowledge_ms: 0,
            focus_ms: 47 * 60_000,
        };
        let response = build_response(
            vec![activity(20_000, "Code", 60)],
            infer_work_mood(&[]),
            &[],
            true,
            totals,
            day,
            day + DAY_MS,
            0,
            false,
        )
        .unwrap();

        let metrics = response.achievement_metrics.unwrap();
        assert_eq!(metrics.coding_minutes, Some(90));
        assert_eq!(metrics.design_minutes, Some(30));
        assert_eq!(metrics.knowledge_minutes, Some(0));
        assert_eq!(metrics.focus_minutes, Some(47));
    }

    #[test]
    fn five_minute_gap_is_continuous_but_larger_gap_interrupts_session() {
        let start = 20_000 * DAY_MS + 9 * 60 * 60 * 1000;
        let continuous = (0..=48)
            .map(|index| start + index * 5 * 60_000)
            .collect::<Vec<_>>();
        let continuous_metrics = build_achievement_metrics(
            &continuous,
            start,
            start + DAY_MS,
            0,
            WorkCategoryTotals::default(),
        );
        assert!(continuous_metrics.longest_work_session_minutes >= 240);

        let interrupted = continuous
            .into_iter()
            .map(|timestamp| {
                if timestamp > start + 60 * 60 * 1000 {
                    timestamp + 60_000
                } else {
                    timestamp
                }
            })
            .collect::<Vec<_>>();
        let interrupted_metrics = build_achievement_metrics(
            &interrupted,
            start,
            start + DAY_MS,
            0,
            WorkCategoryTotals::default(),
        );
        assert!(interrupted_metrics.longest_work_session_minutes < 240);
    }

    #[test]
    fn calculates_current_and_longest_streaks() {
        assert_eq!(streaks(&[10, 11, 13, 14, 15], 15), (3, 3));
        assert_eq!(streaks(&[10, 11, 13, 14, 15], 16), (0, 3));
        assert_eq!(streaks(&[], 16), (0, 0));
    }

    #[test]
    fn infers_focused_mood_from_existing_company_im_captures() {
        let mood = infer_work_mood(&[
            sample("飞书", "我正在排查这个问题，稍后同步结果"),
            sample("Slack", "我来跟进发布计划"),
        ]);

        assert!(mood.inferred);
        assert_eq!(mood.mood, Some(WorkMood::Focused));
        assert_eq!(mood.expression_count, 2);
        assert_eq!(mood.source_apps, vec!["Slack", "飞书"]);
    }

    #[test]
    fn strong_overload_expression_takes_priority() {
        let mood = infer_work_mood(&[
            sample("飞书", "我正在处理，也会继续推进"),
            sample("飞书", "任务太多，已经忙不过来了"),
        ]);

        assert_eq!(mood.mood, Some(WorkMood::Overloaded));
    }
}
