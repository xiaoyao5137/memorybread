//! captures 表的 CRUD 操作
//!
//! 所有方法以 `StorageManager` 的方法形式提供，通过 `with_conn` 持有锁后操作。

use rusqlite::{params, Connection};

use crate::storage::{
    db::current_ts_ms,
    error::StorageError,
    models::{
        CaptureActivityAggregate, CaptureRecord, NewCapture, WorkCategoryTotals,
        WorkImCaptureSample,
    },
    StorageManager,
};

fn keyword_terms(query: &str) -> Vec<String> {
    query
        .split(|ch: char| ch.is_whitespace() || ch.is_ascii_punctuation())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
        .collect()
}

// ─────────────────────────────────────────────────────────────────────────────
// 工作类别时长聚合（内置分类表）
//
// 分类口径：
// 1. 优先按 app_bundle_id 精确归类（小写全等）；
// 2. 浏览器类应用再按窗口标题关键词归类（不区分大小写的包含匹配）；
// 3. 无法归类的时长不进入任何类别。分类表调整时只需修改下面两段 SQL。
// ─────────────────────────────────────────────────────────────────────────────

/// 每个类别的分摊时长：口径与 summarize_capture_activity 一致
/// （相邻采集间隔最多计 idle_gap_cap，末条计 1 分钟）。
const CATEGORY_MINUTES_SQL: &str = "WITH ordered AS (
        SELECT
            id,
            ts,
            LOWER(TRIM(COALESCE(app_bundle_id, ''))) AS bundle_key,
            win_title,
            LEAD(ts) OVER (ORDER BY ts, id) AS next_ts
        FROM captures
        WHERE ts >= ?1 AND ts < ?2 AND is_sensitive = 0
    ), eligible AS (
        SELECT id, ts, bundle_key, win_title, next_ts
        FROM ordered
        WHERE bundle_key != 'com.apple.loginwindow'
    ), categorized AS (
        SELECT
            CASE
                WHEN bundle_key IN (
                    'com.microsoft.vscode',
                    'com.microsoft.vscode.insiders',
                    'com.apple.dt.xcode',
                    'com.apple.terminal',
                    'com.googlecode.iterm2',
                    'dev.warp.warp-macos',
                    'com.sublimetext.4',
                    'com.sublimetext.3',
                    'dev.zed.zed',
                    'com.openai.codex',
                    'com.aliyun.lingma.ide',
                    'com.github.githubdesktop'
                ) OR bundle_key LIKE 'com.jetbrains.%' THEN 'coding'
                WHEN bundle_key IN (
                    'com.figma.desktop',
                    'com.bohemiancoding.sketch3',
                    'com.adobe.photoshop',
                    'com.adobe.illustrator',
                    'com.adobe.xd'
                ) OR bundle_key LIKE 'com.seriflabs.%' THEN 'design'
                WHEN bundle_key IN ('md.obsidian') THEN 'knowledge'
                WHEN bundle_key IN (
                    'com.google.chrome',
                    'com.apple.safari',
                    'com.microsoft.edgemac',
                    'org.mozilla.firefox',
                    'company.arc.browser',
                    'com.brave.browser',
                    'com.vivaldi.vivaldi'
                ) THEN
                    CASE
                        WHEN LOWER(COALESCE(win_title, '')) LIKE '%github%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%gitlab%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%gitee%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%bitbucket%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%stackoverflow%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%jupyter%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%pull request%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%merge request%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%.rs -%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%.py -%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%.ts -%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%.go -%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%.java -%' THEN 'coding'
                        WHEN LOWER(COALESCE(win_title, '')) LIKE '%figma.com%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%mastergo%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%js.design%'
                            OR COALESCE(win_title, '') LIKE '%即时设计%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%canva%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%dribbble%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%behance%' THEN 'design'
                        WHEN COALESCE(win_title, '') LIKE '%语雀%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%yuque%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%notion%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%confluence%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%wikipedia%'
                            OR LOWER(COALESCE(win_title, '')) LIKE '%/wiki/%'
                            OR COALESCE(win_title, '') LIKE '%飞书云文档%' THEN 'knowledge'
                        ELSE NULL
                    END
                ELSE NULL
            END AS category,
            ts,
            CASE
                WHEN next_ts IS NULL THEN 60000
                WHEN next_ts <= ts THEN 0
                WHEN next_ts - ts > ?3 THEN ?3
                ELSE next_ts - ts
            END AS duration_ms
        FROM eligible
    )
    SELECT category, SUM(duration_ms) AS duration_ms
    FROM categorized
    WHERE category IS NOT NULL
    GROUP BY category";

/// 深度专注时长：同一应用连续有效工作（无应用切换、间隔不超 idle_gap）的单段
/// 达到最小长度才计入该段全部分摊时长。
const FOCUS_MINUTES_SQL: &str = "WITH ordered AS (
        SELECT
            id,
            ts,
            COALESCE(
                NULLIF(TRIM(app_bundle_id), ''),
                NULLIF(TRIM(app_name), ''),
                '其他'
            ) AS app_key,
            LEAD(ts) OVER (ORDER BY ts, id) AS next_ts,
            LAG(ts) OVER (ORDER BY ts, id) AS prev_ts,
            LAG(COALESCE(
                NULLIF(TRIM(app_bundle_id), ''),
                NULLIF(TRIM(app_name), ''),
                '其他'
            )) OVER (ORDER BY ts, id) AS prev_app
        FROM captures
        WHERE ts >= ?1 AND ts < ?2 AND is_sensitive = 0
    ), eligible AS (
        SELECT *
        FROM ordered
        WHERE LOWER(TRIM(app_key)) != 'com.apple.loginwindow'
          AND LOWER(TRIM(app_key)) != 'loginwindow'
    ), flagged AS (
        SELECT
            CASE
                WHEN prev_ts IS NULL THEN 1
                WHEN prev_app != app_key THEN 1
                WHEN ts - prev_ts > ?3 THEN 1
                ELSE 0
            END AS run_start,
            CASE
                WHEN next_ts IS NULL THEN 60000
                WHEN next_ts <= ts THEN 0
                WHEN next_ts - ts > ?3 THEN ?3
                ELSE next_ts - ts
            END AS duration_ms,
            ROW_NUMBER() OVER (ORDER BY ts, id) AS seq
        FROM eligible
    ), runs AS (
        SELECT run_start, duration_ms, SUM(run_start) OVER (ORDER BY seq) AS run_id
        FROM flagged
    )
    SELECT COALESCE(SUM(run_duration), 0)
    FROM (
        SELECT run_id, SUM(duration_ms) AS run_duration
        FROM runs
        GROUP BY run_id
        HAVING SUM(duration_ms) >= ?4
    )";

// ─────────────────────────────────────────────────────────────────────────────
// 写操作
// ─────────────────────────────────────────────────────────────────────────────

impl StorageManager {
    /// 插入一条采集记录，返回新行的 id。
    pub fn insert_capture(&self, c: &NewCapture) -> Result<i64, StorageError> {
        self.with_conn(|conn| insert_capture_inner(conn, c))
    }

    /// 删除一条采集记录及其 SQLite 向量元数据。
    ///
    /// 时间线属于已经提炼出的上层资产，因此不会随原始采集记录一起删除。历史
    /// schema 中 `timelines.capture_id` 仍保留外键约束，这里与保留期清理保持同一
    /// 语义，临时关闭外键检查后删除原始记录。
    pub fn delete_capture(&self, id: i64) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let exists = conn.query_row(
                "SELECT EXISTS(SELECT 1 FROM captures WHERE id = ?1)",
                params![id],
                |row| row.get::<_, bool>(0),
            )?;
            if !exists {
                return Ok(false);
            }

            let vector_tx = conn.unchecked_transaction()?;
            vector_tx.execute(
                "INSERT OR IGNORE INTO vector_deletion_queue (
                     qdrant_point_id, source_type, reason, enqueued_at
                 )
                 SELECT
                     qdrant_point_id,
                     source_type,
                     'capture_deleted_by_user',
                     ?2
                 FROM vector_index
                 WHERE capture_id = ?1 AND source_type = 'capture'",
                params![id, current_ts_ms()],
            )?;
            vector_tx.execute(
                "DELETE FROM vector_index
                 WHERE capture_id = ?1 AND source_type = 'capture'",
                params![id],
            )?;
            vector_tx.commit()?;

            conn.execute_batch("PRAGMA foreign_keys = OFF;")?;
            let delete_result = conn.execute("DELETE FROM captures WHERE id = ?1", params![id]);
            let restore_result = conn.execute_batch("PRAGMA foreign_keys = ON;");

            match (delete_result, restore_result) {
                (Ok(affected), Ok(())) => Ok(affected > 0),
                (Err(error), _) => Err(StorageError::Sqlite(error)),
                (Ok(_), Err(error)) => Err(StorageError::Sqlite(error)),
            }
        })
    }

    /// 异步版本（在 spawn_blocking 中执行，不阻塞 tokio 运行时）。
    pub async fn insert_capture_async(&self, c: NewCapture) -> Result<i64, StorageError> {
        self.with_conn_async(move |conn| insert_capture_inner(conn, &c))
            .await
    }

    /// 在 Sidecar 完成 OCR 后，将结果回写到 captures 表。
    pub fn update_ocr_text(
        &self,
        id: i64,
        ocr_text: &str,
        confidence: f32,
    ) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            // confidence 存入 user_preferences 或日志，此处仅更新 ocr_text
            let _ = confidence; // 暂保留以备后续扩展
            conn.execute(
                "UPDATE captures SET ocr_text = ?1 WHERE id = ?2",
                params![ocr_text, id],
            )?;
            Ok(())
        })
    }

    /// 在 Sidecar 完成 ASR 后，将音频转录文本回写。
    pub fn update_audio_text(&self, id: i64, audio_text: &str) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE captures SET audio_text = ?1 WHERE id = ?2",
                params![audio_text, id],
            )?;
            Ok(())
        })
    }

    /// 标记该条记录已完成 PII 脱敏。
    pub fn mark_pii_scrubbed(&self, id: i64) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE captures SET pii_scrubbed = 1 WHERE id = ?1",
                params![id],
            )?;
            Ok(())
        })
    }
}

fn insert_capture_inner(conn: &Connection, c: &NewCapture) -> Result<i64, StorageError> {
    conn.execute(
        "INSERT INTO captures
            (ts, app_name, app_bundle_id, win_title, event_type,
             ax_text, ax_focused_role, ax_focused_id,
             ocr_text, screenshot_path, screenshot_source, input_text, is_sensitive,
             pii_scrubbed, url, webpage_title)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16)",
        params![
            c.ts,
            c.app_name,
            c.app_bundle_id,
            c.win_title,
            c.event_type.as_str(),
            c.ax_text,
            c.ax_focused_role,
            c.ax_focused_id,
            c.ocr_text,
            c.screenshot_path,
            c.screenshot_source,
            c.input_text,
            c.is_sensitive as i64,
            c.pii_scrubbed as i64,
            c.url,
            c.webpage_title,
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

// ─────────────────────────────────────────────────────────────────────────────
// 读操作
// ─────────────────────────────────────────────────────────────────────────────

/// captures 查询过滤条件
#[derive(Debug, Default)]
pub struct CaptureFilter {
    /// 起始时间（Unix ms，含）
    pub from_ts: Option<i64>,
    /// 结束时间（Unix ms，含）
    pub to_ts: Option<i64>,
    /// 按应用名过滤
    pub app_name: Option<String>,
    /// 关键词搜索
    pub query: Option<String>,
    /// 按单个 capture id 限定
    pub capture_id: Option<i64>,
    /// 是否过滤掉隐私记录（默认 true）
    pub exclude_sensitive: bool,
    /// 是否过滤 macOS 登录/锁屏等非工作系统会话（默认 true）
    pub exclude_system_session_apps: bool,
    /// 最多返回条数
    pub limit: usize,
    /// 偏移
    pub offset: usize,
}

impl CaptureFilter {
    pub fn new() -> Self {
        Self {
            exclude_sensitive: true,
            exclude_system_session_apps: true,
            limit: 100,
            ..Default::default()
        }
    }
    pub fn last_24h() -> Self {
        let now = current_ts_ms();
        Self {
            from_ts: Some(now - 86_400_000),
            exclude_sensitive: true,
            exclude_system_session_apps: true,
            limit: 500,
            ..Default::default()
        }
    }
}

impl StorageManager {
    /// 按 id 获取单条记录。
    pub fn get_capture(&self, id: i64) -> Result<Option<CaptureRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, ts, app_name, app_bundle_id, win_title, event_type,
                        ax_text, ax_focused_role, ax_focused_id,
                        ocr_text, screenshot_path, input_text, audio_text,
                        is_sensitive, pii_scrubbed, screenshot_source, url, webpage_title
                 FROM captures WHERE id = ?1",
            )?;
            let mut rows = stmt.query(params![id])?;
            if let Some(row) = rows.next()? {
                Ok(Some(row_to_capture(row)?))
            } else {
                Ok(None)
            }
        })
    }

    /// 按 id 列表批量获取记录。
    pub fn get_captures_by_ids(&self, ids: &[i64]) -> Result<Vec<CaptureRecord>, StorageError> {
        if ids.is_empty() {
            return Ok(vec![]);
        }
        self.with_conn(|conn| {
            let placeholders = ids.iter().map(|_| "?").collect::<Vec<_>>().join(",");
            let sql = format!(
                "SELECT id, ts, app_name, app_bundle_id, win_title, event_type,
                        ax_text, ax_focused_role, ax_focused_id,
                        ocr_text, screenshot_path, input_text, audio_text,
                        is_sensitive, pii_scrubbed, screenshot_source, url, webpage_title
                 FROM captures WHERE id IN ({}) ORDER BY ts",
                placeholders
            );
            let mut stmt = conn.prepare(&sql)?;
            let mut rows = stmt.query(rusqlite::params_from_iter(ids))?;
            let mut result = Vec::new();
            while let Some(row) = rows.next()? {
                result.push(row_to_capture(row)?);
            }
            Ok(result)
        })
    }

    /// 按过滤条件列举采集记录，按 ts 倒序。
    pub fn list_captures(
        &self,
        filter: &CaptureFilter,
    ) -> Result<Vec<CaptureRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT c.id, c.ts, c.app_name, c.app_bundle_id, c.win_title, c.event_type,
                        c.ax_text, c.ax_focused_role, c.ax_focused_id,
                        c.ocr_text, c.screenshot_path, c.input_text, c.audio_text,
                        c.is_sensitive, c.pii_scrubbed, c.screenshot_source, c.url, c.webpage_title
                 FROM captures c",
            );
            let query_terms = filter.query.as_ref().map(|value| keyword_terms(value)).unwrap_or_default();
            sql.push_str(" WHERE ");

            let mut wheres: Vec<String> = Vec::new();
            if !query_terms.is_empty() {
                let query_clause = query_terms
                    .iter()
                    .map(|_| "(COALESCE(c.win_title, '') LIKE ? OR COALESCE(c.webpage_title, '') LIKE ? OR COALESCE(c.url, '') LIKE ? OR COALESCE(c.ax_text, '') LIKE ? OR COALESCE(c.ocr_text, '') LIKE ? OR COALESCE(c.input_text, '') LIKE ? OR COALESCE(c.audio_text, '') LIKE ?)".to_string())
                    .collect::<Vec<_>>()
                    .join(" AND ");
                wheres.push(format!("({})", query_clause));
            }
            if filter.from_ts.is_some() { wheres.push("c.ts >= ?".into()); }
            if filter.to_ts.is_some() { wheres.push("c.ts <= ?".into()); }
            if filter.app_name.is_some() { wheres.push("c.app_name = ?".into()); }
            if filter.capture_id.is_some() { wheres.push("c.id = ?".into()); }
            if filter.exclude_sensitive { wheres.push("c.is_sensitive = 0".into()); }
            if filter.exclude_system_session_apps {
                wheres.push(
                    "LOWER(TRIM(COALESCE(c.app_bundle_id, ''))) != 'com.apple.loginwindow'
                     AND LOWER(TRIM(COALESCE(c.app_name, ''))) != 'loginwindow'"
                        .into(),
                );
            }

            let where_clause = if wheres.is_empty() { "1=1".to_string() } else { wheres.join(" AND ") };
            sql.push_str(&where_clause);
            sql.push_str(" ORDER BY c.ts DESC LIMIT ? OFFSET ?");

            let mut stmt = conn.prepare(&sql)?;
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
            for term in &query_terms {
                let pattern = format!("%{}%", term);
                for _ in 0..7 {
                    bind_values.push(Box::new(pattern.clone()));
                }
            }
            if let Some(v) = filter.from_ts { bind_values.push(Box::new(v)); }
            if let Some(v) = filter.to_ts { bind_values.push(Box::new(v)); }
            if let Some(ref v) = filter.app_name { bind_values.push(Box::new(v.clone())); }
            if let Some(v) = filter.capture_id { bind_values.push(Box::new(v)); }
            bind_values.push(Box::new(filter.limit as i64));
            bind_values.push(Box::new(filter.offset as i64));

            let params: Vec<&dyn rusqlite::ToSql> = bind_values.iter().map(|b| b.as_ref()).collect();
            let rows = stmt.query_map(params.as_slice(), |row| {
                Ok(row_to_capture(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;

            rows.collect::<Result<Vec<_>, _>>().map_err(StorageError::Sqlite)
        })
    }

    pub fn count_captures(&self, filter: &CaptureFilter) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from("SELECT COUNT(*) FROM captures c");
            let query_terms = filter.query.as_ref().map(|value| keyword_terms(value)).unwrap_or_default();
            sql.push_str(" WHERE ");

            let mut wheres: Vec<String> = Vec::new();
            if !query_terms.is_empty() {
                let query_clause = query_terms
                    .iter()
                    .map(|_| "(COALESCE(c.win_title, '') LIKE ? OR COALESCE(c.webpage_title, '') LIKE ? OR COALESCE(c.url, '') LIKE ? OR COALESCE(c.ax_text, '') LIKE ? OR COALESCE(c.ocr_text, '') LIKE ? OR COALESCE(c.input_text, '') LIKE ? OR COALESCE(c.audio_text, '') LIKE ?)".to_string())
                    .collect::<Vec<_>>()
                    .join(" AND ");
                wheres.push(format!("({})", query_clause));
            }
            if filter.from_ts.is_some() { wheres.push("c.ts >= ?".into()); }
            if filter.to_ts.is_some() { wheres.push("c.ts <= ?".into()); }
            if filter.app_name.is_some() { wheres.push("c.app_name = ?".into()); }
            if filter.capture_id.is_some() { wheres.push("c.id = ?".into()); }
            if filter.exclude_sensitive { wheres.push("c.is_sensitive = 0".into()); }
            if filter.exclude_system_session_apps {
                wheres.push(
                    "LOWER(TRIM(COALESCE(c.app_bundle_id, ''))) != 'com.apple.loginwindow'
                     AND LOWER(TRIM(COALESCE(c.app_name, ''))) != 'loginwindow'"
                        .into(),
                );
            }

            let where_clause = if wheres.is_empty() { "1=1".to_string() } else { wheres.join(" AND ") };
            sql.push_str(&where_clause);

            let mut stmt = conn.prepare(&sql)?;
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
            for term in &query_terms {
                let pattern = format!("%{}%", term);
                for _ in 0..7 {
                    bind_values.push(Box::new(pattern.clone()));
                }
            }
            if let Some(v) = filter.from_ts { bind_values.push(Box::new(v)); }
            if let Some(v) = filter.to_ts { bind_values.push(Box::new(v)); }
            if let Some(ref v) = filter.app_name { bind_values.push(Box::new(v.clone())); }
            if let Some(v) = filter.capture_id { bind_values.push(Box::new(v)); }
            let params: Vec<&dyn rusqlite::ToSql> = bind_values.iter().map(|b| b.as_ref()).collect();

            stmt.query_row(params.as_slice(), |row| row.get(0)).map_err(StorageError::Sqlite)
        })
    }

    pub fn search_captures_paginated(
        &self,
        query: &str,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<CaptureRecord>, StorageError> {
        let mut filter = CaptureFilter::new();
        filter.query = Some(query.to_string());
        filter.limit = limit;
        filter.offset = offset;
        self.list_captures(&filter)
    }

    pub fn count_search_captures(&self, query: &str) -> Result<i64, StorageError> {
        let mut filter = CaptureFilter::new();
        filter.query = Some(query.to_string());
        self.count_captures(&filter)
    }

    /// 按本地日期和应用聚合工作活动。
    ///
    /// 相邻采集记录之间最多计入 `idle_gap_cap_ms`，避免长时间离开电脑被计为工作。
    /// 末条记录按一分钟计入。敏感采集记录不会进入统计，也不会暴露窗口正文。
    pub fn summarize_capture_activity(
        &self,
        from_ts: i64,
        to_ts: i64,
        timezone_offset_ms: i64,
        idle_gap_cap_ms: i64,
    ) -> Result<Vec<CaptureActivityAggregate>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "WITH ordered AS (
                    SELECT
                        id,
                        ts,
                        COALESCE(NULLIF(TRIM(app_name), ''), '其他') AS app_name,
                        app_bundle_id,
                        LEAD(ts) OVER (ORDER BY ts, id) AS next_ts
                    FROM captures
                    WHERE ts >= ?1 AND ts < ?2 AND is_sensitive = 0
                ), eligible AS (
                    SELECT
                        id,
                        ts,
                        app_name,
                        next_ts,
                        LAG(ts) OVER (ORDER BY ts, id) AS previous_work_ts
                    FROM ordered
                    WHERE LOWER(TRIM(COALESCE(app_bundle_id, ''))) != 'com.apple.loginwindow'
                      AND LOWER(TRIM(app_name)) != 'loginwindow'
                ), apportioned AS (
                    SELECT
                        CAST((ts + ?3) / 86400000 AS INTEGER) AS day_index,
                        app_name,
                        ts,
                        CASE
                            WHEN next_ts IS NULL THEN 60000
                            WHEN next_ts <= ts THEN 0
                            WHEN next_ts - ts > ?4 THEN ?4
                            ELSE next_ts - ts
                        END AS duration_ms,
                        CASE
                            WHEN previous_work_ts IS NULL THEN 1
                            WHEN CAST((previous_work_ts + ?3) / 86400000 AS INTEGER)
                                 != CAST((ts + ?3) / 86400000 AS INTEGER) THEN 1
                            WHEN ts - previous_work_ts > ?4 THEN 1
                            ELSE 0
                        END AS starts_active_period
                    FROM eligible
                )
                SELECT
                    day_index,
                    app_name,
                    SUM(duration_ms) AS duration_ms,
                    COUNT(*) AS capture_count,
                    SUM(starts_active_period) AS active_period_count,
                    MIN(ts) AS first_ts,
                    MAX(ts) AS last_ts
                FROM apportioned
                GROUP BY day_index, app_name
                ORDER BY day_index ASC, duration_ms DESC, app_name ASC",
            )?;

            let rows = stmt.query_map(
                params![from_ts, to_ts, timezone_offset_ms, idle_gap_cap_ms],
                |row| {
                    Ok(CaptureActivityAggregate {
                        day_index: row.get(0)?,
                        app_name: row.get(1)?,
                        duration_ms: row.get(2)?,
                        capture_count: row.get(3)?,
                        active_period_count: row.get(4)?,
                        first_ts: row.get(5)?,
                        last_ts: row.get(6)?,
                    })
                },
            )?;

            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    /// 按内置工作类别聚合本地有效工作时长。
    ///
    /// 分类完全在本机完成：先按 `app_bundle_id` 精确表归类，浏览器类应用再看窗口标题关键词；
    /// 时长分摊口径与 [`Self::summarize_capture_activity`] 完全一致。只返回分钟级聚合结果，
    /// 不暴露窗口标题或采集正文。
    ///
    /// `focus` 定义为“同一应用连续有效工作不切换”的长段：单段时长达到
    /// `min_focus_run_ms` 才计入，用于支撑“深度专注”类任务口径。
    pub fn summarize_work_category_minutes(
        &self,
        from_ts: i64,
        to_ts: i64,
        idle_gap_cap_ms: i64,
        min_focus_run_ms: i64,
    ) -> Result<WorkCategoryTotals, StorageError> {
        let category_rows = self.with_conn(|conn| {
            let mut stmt = conn.prepare(CATEGORY_MINUTES_SQL)?;
            let rows = stmt.query_map(params![from_ts, to_ts, idle_gap_cap_ms], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })?;

        // 深度专注统计失败时降级为 0，不影响其它类别的正常下发。
        let focus_ms = self
            .with_conn(|conn| {
                let mut stmt = conn.prepare(FOCUS_MINUTES_SQL)?;
                stmt.query_row(
                    params![from_ts, to_ts, idle_gap_cap_ms, min_focus_run_ms],
                    |row| row.get::<_, i64>(0),
                )
                .map_err(StorageError::Sqlite)
            })
            .unwrap_or_default();

        let mut totals = WorkCategoryTotals {
            focus_ms: focus_ms.max(0),
            ..Default::default()
        };
        for (category, duration_ms) in category_rows {
            let slot = match category.as_str() {
                "coding" => &mut totals.coding_ms,
                "design" => &mut totals.design_ms,
                "knowledge" => &mut totals.knowledge_ms,
                _ => continue,
            };
            *slot += duration_ms.max(0);
        }
        Ok(totals)
    }

    /// 读取工作画像时间段内的有效采集时间点。
    ///
    /// 该接口只返回时间戳，用于在本地计算连续工作和夜间工作峰值；
    /// 不读取或返回应用名、窗口标题、截图及正文内容。
    pub fn list_capture_activity_timestamps(
        &self,
        from_ts: i64,
        to_ts: i64,
    ) -> Result<Vec<i64>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT ts
                 FROM captures
                 WHERE ts >= ?1
                   AND ts < ?2
                   AND is_sensitive = 0
                   AND LOWER(TRIM(COALESCE(app_bundle_id, ''))) != 'com.apple.loginwindow'
                   AND LOWER(TRIM(COALESCE(app_name, ''))) != 'loginwindow'
                 ORDER BY ts ASC, id ASC",
            )?;
            let rows = stmt.query_map(params![from_ts, to_ts], |row| row.get(0))?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    /// 读取指定时段内已经落库的企业 IM 采集文本，用于本地心情推断。
    ///
    /// 不发起任何额外采集，只复用已经经过采集隐私门禁和内容脱敏的 `ax_text/ocr_text`。
    /// 查询时还会再次应用当前应用黑名单，因此用户后来关闭某个企业 IM 的采集后，
    /// 该应用的历史记录也不会继续参与分析。
    pub fn list_enabled_company_im_capture_samples(
        &self,
        from_ts: i64,
        to_ts: i64,
        limit: usize,
    ) -> Result<Vec<WorkImCaptureSample>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "WITH eligible AS (
                    SELECT
                        id,
                        ts,
                        COALESCE(NULLIF(TRIM(app_name), ''), '企业 IM') AS app_name,
                        app_bundle_id,
                        COALESCE(
                            NULLIF(TRIM(ax_text), ''),
                            NULLIF(TRIM(ocr_text), '')
                        ) AS sample_text
                    FROM captures
                    WHERE ts >= ?1
                      AND ts < ?2
                      AND is_sensitive = 0
                 )
                 SELECT
                    c.app_name,
                    SUBSTR(c.sample_text, 1, 500) AS sample_text
                 FROM eligible c
                 WHERE c.sample_text IS NOT NULL
                   AND c.sample_text != '[已过滤]'
                   AND (
                       LOWER(c.app_name) LIKE '%feishu%'
                       OR c.app_name LIKE '%飞书%'
                       OR LOWER(c.app_name) LIKE '%lark%'
                       OR LOWER(c.app_name) LIKE '%dingtalk%'
                       OR c.app_name LIKE '%钉钉%'
                       OR LOWER(c.app_name) LIKE '%wecom%'
                       OR LOWER(c.app_name) LIKE '%wework%'
                       OR c.app_name LIKE '%企业微信%'
                       OR LOWER(c.app_name) LIKE '%slack%'
                       OR LOWER(c.app_name) LIKE '%teams%'
                       OR LOWER(COALESCE(c.app_bundle_id, '')) LIKE '%feishu%'
                       OR LOWER(COALESCE(c.app_bundle_id, '')) LIKE '%lark%'
                       OR LOWER(COALESCE(c.app_bundle_id, '')) LIKE '%dingtalk%'
                       OR LOWER(COALESCE(c.app_bundle_id, '')) LIKE '%wecom%'
                       OR LOWER(COALESCE(c.app_bundle_id, '')) LIKE '%wework%'
                       OR LOWER(COALESCE(c.app_bundle_id, '')) LIKE '%slack%'
                       OR LOWER(COALESCE(c.app_bundle_id, '')) LIKE '%teams%'
                   )
                   AND NOT EXISTS (
                       SELECT 1
                       FROM app_blacklist blocked
                       WHERE blocked.enabled = 1
                         AND (
                             (
                                 TRIM(blocked.bundle_id) != ''
                                 AND LOWER(TRIM(blocked.bundle_id)) =
                                     LOWER(TRIM(COALESCE(c.app_bundle_id, '')))
                             )
                             OR (
                                 TRIM(blocked.app_name) != ''
                                 AND LOWER(TRIM(blocked.app_name)) =
                                     LOWER(TRIM(c.app_name))
                             )
                         )
                   )
                 ORDER BY c.ts ASC, c.id ASC
                 LIMIT ?3",
            )?;

            let rows = stmt.query_map(
                params![from_ts, to_ts, i64::try_from(limit.min(500)).unwrap_or(500)],
                |row| {
                    Ok(WorkImCaptureSample {
                        app_name: row.get(0)?,
                        text: row.get(1)?,
                    })
                },
            )?;

            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    /// 查询一批 capture 各自所属时间线的 (timeline_id, summary)。
    ///
    /// 走 captures.timeline_id → timelines 直连，能正确覆盖被合并到时间线的从属
    /// capture（leader/follower 都能查到归属）。
    pub fn list_capture_timeline_links(
        &self,
        capture_ids: &[i64],
    ) -> Result<std::collections::HashMap<i64, (i64, String)>, StorageError> {
        if capture_ids.is_empty() {
            return Ok(std::collections::HashMap::new());
        }

        self.with_conn(|conn| {
            let placeholders = std::iter::repeat("?")
                .take(capture_ids.len())
                .collect::<Vec<_>>()
                .join(", ");
            let sql = format!(
                "SELECT c.id, t.id, t.summary
                 FROM captures c
                 JOIN timelines t ON t.id = c.timeline_id
                 WHERE c.id IN ({}) AND c.timeline_id IS NOT NULL",
                placeholders
            );
            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> = capture_ids
                .iter()
                .map(|id| id as &dyn rusqlite::ToSql)
                .collect();
            let rows = stmt.query_map(params.as_slice(), |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })?;

            let mut result = std::collections::HashMap::new();
            for row in rows {
                let (capture_id, timeline_id, summary) = row?;
                result.entry(capture_id).or_insert((timeline_id, summary));
            }
            Ok(result)
        })
    }

    /// 简单列举最近的 N 条采集记录（用于调试面板）。
    pub fn list_recent(
        &self,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<CaptureRecord>, StorageError> {
        let filter = CaptureFilter {
            exclude_sensitive: false,
            limit,
            offset,
            ..Default::default()
        };
        self.list_captures(&filter)
    }

    /// 统计总采集数（用于调试面板）。
    pub fn count(&self) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let count: i64 =
                conn.query_row("SELECT COUNT(*) FROM captures", [], |row| row.get(0))?;
            Ok(count)
        })
    }

    /// FTS5 全文检索（关键词搜索）。
    pub fn search_captures(
        &self,
        query: &str,
        limit: usize,
    ) -> Result<Vec<CaptureRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT c.id, c.ts, c.app_name, c.app_bundle_id, c.win_title, c.event_type,
                        c.ax_text, c.ax_focused_role, c.ax_focused_id,
                        c.ocr_text, c.screenshot_path, c.input_text, c.audio_text,
                        c.is_sensitive, c.pii_scrubbed, c.screenshot_source, c.url, c.webpage_title
                 FROM captures c
                 JOIN captures_fts f ON f.rowid = c.id
                 WHERE captures_fts MATCH ?1
                   AND c.is_sensitive = 0
                 ORDER BY rank
                 LIMIT ?2",
            )?;
            let rows = stmt.query_map(params![query, limit as i64], |row| {
                Ok(row_to_capture(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 行映射辅助
// ─────────────────────────────────────────────────────────────────────────────

fn row_to_capture(row: &rusqlite::Row<'_>) -> Result<CaptureRecord, StorageError> {
    Ok(CaptureRecord {
        id: row.get(0)?,
        ts: row.get(1)?,
        app_name: row.get(2)?,
        app_bundle_id: row.get(3)?,
        win_title: row.get(4)?,
        event_type: row.get(5)?,
        ax_text: row.get(6)?,
        ax_focused_role: row.get(7)?,
        ax_focused_id: row.get(8)?,
        ocr_text: row.get(9)?,
        screenshot_path: row.get(10)?,
        input_text: row.get(11)?,
        audio_text: row.get(12)?,
        is_sensitive: row.get::<_, i64>(13)? != 0,
        pii_scrubbed: row.get::<_, i64>(14)? != 0,
        screenshot_source: row.get(15)?,
        url: row.get(16)?,
        webpage_title: row.get(17)?,
    })
}

// ─────────────────────────────────────────────────────────────────────────────
// 测试
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::models::EventType;

    fn make_mgr() -> StorageManager {
        StorageManager::open_in_memory().expect("内存数据库初始化失败")
    }

    fn sample_capture() -> NewCapture {
        NewCapture {
            ts: 1_700_000_000_000,
            app_name: Some("Feishu".into()),
            app_bundle_id: Some("com.feishu.feishu".into()),
            win_title: Some("飞书 - 工作群".into()),
            event_type: EventType::MouseClick,
            ax_text: Some("欢迎使用飞书".into()),
            ax_focused_role: Some("AXTextField".into()),
            ax_focused_id: Some("input-1".into()),
            ocr_text: None,
            screenshot_path: Some("2026/03/04/test.jpg".into()),
            screenshot_source: Some("window".into()),
            input_text: Some("你好".into()),
            is_sensitive: false,
            pii_scrubbed: false,
            url: None,
            webpage_title: None,
        }
    }

    #[test]
    fn test_insert_and_get() {
        let mgr = make_mgr();
        let id = mgr.insert_capture(&sample_capture()).unwrap();
        assert!(id > 0);

        let rec = mgr.get_capture(id).unwrap().expect("记录应存在");
        assert_eq!(rec.app_name.as_deref(), Some("Feishu"));
        assert_eq!(rec.ax_text.as_deref(), Some("欢迎使用飞书"));
        assert_eq!(rec.best_text(), Some("欢迎使用飞书"));
    }

    #[test]
    fn test_update_ocr_text() {
        let mgr = make_mgr();
        let id = mgr.insert_capture(&sample_capture()).unwrap();
        mgr.update_ocr_text(id, "OCR识别的文字", 0.92).unwrap();

        let rec = mgr.get_capture(id).unwrap().unwrap();
        assert_eq!(rec.ocr_text.as_deref(), Some("OCR识别的文字"));
    }

    #[test]
    fn test_list_captures() {
        let mgr = make_mgr();
        mgr.insert_capture(&sample_capture()).unwrap();
        mgr.insert_capture(&sample_capture()).unwrap();

        let filter = CaptureFilter::new();
        let list = mgr.list_captures(&filter).unwrap();
        assert_eq!(list.len(), 2);
    }

    #[test]
    fn test_fts_search() {
        let mgr = make_mgr();
        mgr.insert_capture(&sample_capture()).unwrap();

        // unicode61 分词器把连续汉字视为整个 token，"你好"存入 input_text，
        // FTS 对精确 token 的查询能命中。
        let results = mgr.search_captures("你好", 10).unwrap();
        assert!(!results.is_empty(), "FTS 应找到包含'你好'的记录");
    }

    #[test]
    fn test_sensitive_capture_excluded() {
        let mgr = make_mgr();
        let mut c = sample_capture();
        c.is_sensitive = true;
        c.ax_text = None;
        mgr.insert_capture(&c).unwrap();

        let filter = CaptureFilter::new(); // exclude_sensitive = true
        let list = mgr.list_captures(&filter).unwrap();
        assert_eq!(list.len(), 0, "敏感记录应被过滤");
    }

    #[test]
    fn test_summarize_capture_activity_caps_idle_time_and_excludes_sensitive_rows() {
        let mgr = make_mgr();
        let base = 1_700_000_000_000_i64;

        let mut first = sample_capture();
        first.ts = base;
        first.app_name = Some("Code".into());
        mgr.insert_capture(&first).unwrap();

        let mut second = sample_capture();
        second.ts = base + 2 * 60_000;
        second.app_name = Some("Code".into());
        mgr.insert_capture(&second).unwrap();

        let mut third = sample_capture();
        third.ts = base + 10 * 60_000;
        third.app_name = Some("Browser".into());
        mgr.insert_capture(&third).unwrap();

        let mut sensitive = sample_capture();
        sensitive.ts = base + 11 * 60_000;
        sensitive.app_name = Some("Private Chat".into());
        sensitive.is_sensitive = true;
        mgr.insert_capture(&sensitive).unwrap();

        let rows = mgr
            .summarize_capture_activity(base - 1, base + 20 * 60_000, 0, 5 * 60_000)
            .unwrap();

        let code = rows.iter().find(|row| row.app_name == "Code").unwrap();
        let browser = rows.iter().find(|row| row.app_name == "Browser").unwrap();
        assert_eq!(code.duration_ms, 7 * 60_000);
        assert_eq!(code.capture_count, 2);
        assert_eq!(browser.duration_ms, 60_000);
        assert!(!rows.iter().any(|row| row.app_name == "Private Chat"));
    }

    #[test]
    fn test_work_activity_and_capture_list_exclude_loginwindow() {
        let mgr = make_mgr();
        let base = 1_700_000_000_000_i64;

        let mut editor = sample_capture();
        editor.ts = base;
        editor.app_name = Some("Code".into());
        editor.app_bundle_id = Some("com.microsoft.VSCode".into());
        mgr.insert_capture(&editor).unwrap();

        let mut locked = sample_capture();
        locked.ts = base + 60_000;
        locked.app_name = Some("loginwindow".into());
        locked.app_bundle_id = Some("com.apple.loginwindow".into());
        mgr.insert_capture(&locked).unwrap();

        let mut locked_again = locked.clone();
        locked_again.ts = base + 4 * 60_000;
        mgr.insert_capture(&locked_again).unwrap();

        let mut browser = sample_capture();
        browser.ts = base + 10 * 60_000;
        browser.app_name = Some("Browser".into());
        browser.app_bundle_id = Some("com.example.browser".into());
        mgr.insert_capture(&browser).unwrap();

        let rows = mgr
            .summarize_capture_activity(base, base + 20 * 60_000, 0, 5 * 60_000)
            .unwrap();

        assert_eq!(rows.iter().map(|row| row.capture_count).sum::<i64>(), 2);
        assert_eq!(
            rows.iter().map(|row| row.duration_ms).sum::<i64>(),
            2 * 60_000
        );
        assert_eq!(
            rows.iter().map(|row| row.active_period_count).sum::<i64>(),
            2
        );
        assert!(!rows.iter().any(|row| row.app_name == "loginwindow"));

        let listed = mgr.list_captures(&CaptureFilter::new()).unwrap();
        assert_eq!(listed.len(), 2);
        assert!(!listed
            .iter()
            .any(|capture| { capture.app_bundle_id.as_deref() == Some("com.apple.loginwindow") }));
    }

    #[test]
    fn test_list_capture_activity_timestamps_returns_only_non_sensitive_rows() {
        let mgr = make_mgr();
        let base = 1_700_000_000_000_i64;

        let mut first = sample_capture();
        first.ts = base + 2;
        mgr.insert_capture(&first).unwrap();

        let mut second = sample_capture();
        second.ts = base + 1;
        mgr.insert_capture(&second).unwrap();

        let mut sensitive = sample_capture();
        sensitive.ts = base + 3;
        sensitive.is_sensitive = true;
        mgr.insert_capture(&sensitive).unwrap();

        let timestamps = mgr
            .list_capture_activity_timestamps(base, base + 4)
            .unwrap();

        assert_eq!(timestamps, vec![base + 1, base + 2]);
    }

    fn capture_at(ts: i64, bundle: &str, title: &str) -> NewCapture {
        let mut capture = sample_capture();
        capture.ts = ts;
        capture.app_name = Some("TestApp".into());
        capture.app_bundle_id = Some(bundle.into());
        capture.win_title = Some(title.into());
        capture
    }

    #[test]
    fn category_minutes_classify_by_bundle_and_browser_title() {
        let mgr = make_mgr();
        let base = 1_700_000_000_000_i64;
        let minute = 60_000_i64;
        let idle_cap = 5 * minute;

        // VSCode（大写 Bundle 验证归一化）两条，间隔 10 分钟，各按 5 分钟封顶计入。
        mgr.insert_capture(&capture_at(base, "com.microsoft.VSCode", "main.rs - demo"))
            .unwrap();
        mgr.insert_capture(&capture_at(
            base + 10 * minute,
            "com.microsoft.vscode",
            "main.rs - demo",
        ))
        .unwrap();
        // 浏览器标题命中 coding 关键词；另一条浏览器标题未命中任何类别。
        mgr.insert_capture(&capture_at(
            base + 20 * minute,
            "com.google.Chrome",
            "GitHub - Pull Request #12",
        ))
        .unwrap();
        mgr.insert_capture(&capture_at(
            base + 30 * minute,
            "com.google.chrome",
            "娱乐视频站点",
        ))
        .unwrap();
        // Obsidian 按 bundle 归入 knowledge，末条计 1 分钟。
        mgr.insert_capture(&capture_at(base + 40 * minute, "md.obsidian", "工作笔记"))
            .unwrap();

        let totals = mgr
            .summarize_work_category_minutes(base, base + 60 * minute, idle_cap, 45 * minute)
            .unwrap();

        assert_eq!(totals.coding_ms, 15 * minute);
        assert_eq!(totals.knowledge_ms, minute);
        assert_eq!(totals.design_ms, 0);
        // 每条都是不同应用的单条短段，达不到深度专注门槛。
        assert_eq!(totals.focus_ms, 0);
    }

    #[test]
    fn focus_minutes_require_a_single_app_run_above_the_threshold() {
        let mgr = make_mgr();
        let base = 1_700_000_000_000_i64;
        let minute = 60_000_i64;
        let idle_cap = 5 * minute;

        // 同一应用连续 10 条、间隔恰好 5 分钟：9 段间隔各封顶 5 分钟，末条计 1 分钟，
        // 总分摊 46 分钟 ≥ 45 分钟门槛。
        for index in 0..10 {
            mgr.insert_capture(&capture_at(
                base + index * idle_cap,
                "com.microsoft.vscode",
                "edit.rs - demo",
            ))
            .unwrap();
        }

        let totals = mgr
            .summarize_work_category_minutes(base, base + 60 * minute, idle_cap, 45 * minute)
            .unwrap();
        assert_eq!(totals.focus_ms, 9 * idle_cap + minute);
        assert_eq!(totals.coding_ms, 9 * idle_cap + minute);

        // 中间切换到其它应用会切断深度专注段。
        let interrupted_mgr = make_mgr();
        for index in 0..5 {
            interrupted_mgr
                .insert_capture(&capture_at(
                    base + index * idle_cap,
                    "com.microsoft.vscode",
                    "edit.rs",
                ))
                .unwrap();
        }
        interrupted_mgr
            .insert_capture(&capture_at(
                base + 5 * idle_cap,
                "com.apple.Safari",
                "GitHub",
            ))
            .unwrap();
        for index in 6..10 {
            interrupted_mgr
                .insert_capture(&capture_at(
                    base + index * idle_cap,
                    "com.microsoft.vscode",
                    "edit.rs",
                ))
                .unwrap();
        }
        let interrupted = interrupted_mgr
            .summarize_work_category_minutes(base, base + 60 * minute, idle_cap, 45 * minute)
            .unwrap();
        assert_eq!(interrupted.focus_ms, 0);
    }

    #[test]
    fn category_minutes_ignore_sensitive_captures() {
        let mgr = make_mgr();
        let base = 1_700_000_000_000_i64;
        let minute = 60_000_i64;

        let mut sensitive = capture_at(base, "com.microsoft.vscode", "secret.rs");
        sensitive.is_sensitive = true;
        mgr.insert_capture(&sensitive).unwrap();

        let totals = mgr
            .summarize_work_category_minutes(base, base + 60 * minute, 5 * minute, 45 * minute)
            .unwrap();
        assert_eq!(totals, WorkCategoryTotals::default());
    }

    #[test]
    fn test_company_im_samples_reuse_existing_filtered_captures_and_current_blacklist() {
        let mgr = make_mgr();
        let base = 1_700_000_000_000_i64;

        let mut feishu = sample_capture();
        feishu.ts = base;
        feishu.app_name = Some("飞书".into());
        feishu.ax_text = Some("正在处理，稍后同步结果".into());
        feishu.input_text = Some("不应依赖专用输入字段".into());
        mgr.insert_capture(&feishu).unwrap();

        let mut slack = sample_capture();
        slack.ts = base + 1;
        slack.app_name = Some("Slack".into());
        slack.app_bundle_id = Some("com.tinyspeck.slackmacgap".into());
        slack.ax_text = None;
        slack.ocr_text = Some("发布已经完成".into());
        mgr.insert_capture(&slack).unwrap();

        let mut teams = sample_capture();
        teams.ts = base + 2;
        teams.app_name = Some("Microsoft Teams".into());
        teams.app_bundle_id = Some("com.microsoft.teams2".into());
        teams.ax_text = Some("Teams 中的工作消息".into());
        mgr.insert_capture(&teams).unwrap();

        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO app_blacklist (bundle_id, app_name, enabled, reason)
                 VALUES ('com.microsoft.teams2', 'Microsoft Teams', 1, '用户关闭采集')",
                [],
            )?;
            Ok(())
        })
        .unwrap();

        let mut editor = sample_capture();
        editor.ts = base + 3;
        editor.app_name = Some("Code".into());
        editor.app_bundle_id = Some("com.microsoft.VSCode".into());
        editor.ax_text = Some("非 IM 内容".into());
        mgr.insert_capture(&editor).unwrap();

        let mut personal_im = sample_capture();
        personal_im.ts = base + 4;
        personal_im.app_name = Some("Discord".into());
        personal_im.app_bundle_id = Some("com.hnc.Discord".into());
        personal_im.ax_text = Some("不属于企业 IM 范围".into());
        mgr.insert_capture(&personal_im).unwrap();

        let mut sensitive = sample_capture();
        sensitive.ts = base + 5;
        sensitive.app_name = Some("企业微信".into());
        sensitive.app_bundle_id = Some("com.tencent.WeWorkMac".into());
        sensitive.ax_text = Some("敏感内容".into());
        sensitive.is_sensitive = true;
        mgr.insert_capture(&sensitive).unwrap();

        let samples = mgr
            .list_enabled_company_im_capture_samples(base - 1, base + 10, 200)
            .unwrap();

        assert_eq!(samples.len(), 2);
        assert_eq!(samples[0].app_name, "飞书");
        assert_eq!(samples[0].text, "正在处理，稍后同步结果");
        assert_eq!(samples[1].app_name, "Slack");
        assert_eq!(samples[1].text, "发布已经完成");
        assert!(!samples
            .iter()
            .any(|sample| sample.text.contains("专用输入")));
        assert!(!samples
            .iter()
            .any(|sample| sample.app_name == "Microsoft Teams"));

        mgr.with_conn(|conn| {
            conn.execute(
                "UPDATE app_blacklist SET enabled = 0
                 WHERE bundle_id = 'com.microsoft.teams2'",
                [],
            )?;
            Ok(())
        })
        .unwrap();
        let enabled_samples = mgr
            .list_enabled_company_im_capture_samples(base - 1, base + 10, 200)
            .unwrap();
        assert!(enabled_samples
            .iter()
            .any(|sample| sample.app_name == "Microsoft Teams"));
    }
}
