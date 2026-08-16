use rusqlite::{params, Connection};

use crate::storage::{
    db::current_ts_ms,
    document_identity::{
        canonical_document_identity, canonical_document_source_title,
        canonical_document_title_identity,
    },
    error::StorageError,
    fts::{build_fts_or_query, fts_candidate_ids, render_in_clause, DEFAULT_FTS_CANDIDATE_CAP},
    models_bake::{
        BakeActionTraceRecord, BakeDocumentRecord, BakeKnowledgeRecord, BakeMemorySourceRecord,
        BakeSopRecord, EpisodicMemoryRecord, NewBakeKnowledge, NewBakeSop, NewEpisodicMemory,
        NewTimeline, TimelineRecord,
    },
    StorageManager,
};

fn keyword_terms(query: &str) -> Vec<String> {
    let mut terms = query
        .split(|ch: char| ch.is_whitespace() || ch.is_ascii_punctuation())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
        .collect::<Vec<_>>();

    if terms.len() == 1 && terms[0].chars().count() >= 5 {
        let chars = terms[0].chars().collect::<Vec<_>>();
        if chars.iter().any(|ch| !ch.is_ascii()) {
            for window in chars.windows(2) {
                let term = window.iter().collect::<String>();
                if !terms.contains(&term) {
                    terms.push(term);
                }
            }
        }
    }

    terms
}

#[derive(Debug)]
struct TimelineUrlGroup {
    identity: String,
    representative_url: String,
    title_affinity: u8,
    occurrence_count: usize,
    last_seen_ts: i64,
    is_document: bool,
}

fn normalized_source_title(value: &str) -> String {
    value.trim().to_lowercase()
}

fn source_title_affinity(candidate: Option<&str>, preferred_titles: &[&str]) -> u8 {
    let Some(candidate) = candidate
        .map(normalized_source_title)
        .filter(|value| !value.is_empty())
    else {
        return 0;
    };

    preferred_titles
        .iter()
        .map(|preferred| normalized_source_title(preferred))
        .filter(|preferred| !preferred.is_empty())
        .map(|preferred| {
            if candidate == preferred {
                3
            } else if candidate.chars().count().min(preferred.chars().count()) >= 6
                && (candidate.contains(&preferred) || preferred.contains(&candidate))
            {
                2
            } else {
                0
            }
        })
        .max()
        .unwrap_or(0)
}

fn prefer_representative_url(current: &str, candidate: &str) -> bool {
    let current_has_fragment = current.contains('#');
    let candidate_has_fragment = candidate.contains('#');
    (current_has_fragment && !candidate_has_fragment)
        || (current_has_fragment == candidate_has_fragment && candidate.len() < current.len())
}

fn is_generic_document_landing_url(url: &str) -> bool {
    let without_fragment = url.split('#').next().unwrap_or(url);
    let without_query = without_fragment
        .split('?')
        .next()
        .unwrap_or(without_fragment);
    let without_protocol = without_query
        .strip_prefix("https://")
        .or_else(|| without_query.strip_prefix("http://"))
        .unwrap_or(without_query);
    let path = without_protocol
        .split_once('/')
        .map(|(_, path)| path.trim_matches('/'))
        .unwrap_or("");
    path.is_empty() || matches!(path.to_lowercase().as_str(), "home" | "docs" | "wiki")
}

fn find_timeline_fallback_source_url(
    conn: &Connection,
    timeline_id: i64,
    preferred_titles: &[&str],
) -> Result<Option<String>, StorageError> {
    let mut stmt = conn.prepare(
        "SELECT url, win_title, webpage_title, ts
         FROM captures
         WHERE timeline_id = ?1
           AND url IS NOT NULL
           AND TRIM(url) <> ''
         ORDER BY ts ASC, id ASC",
    )?;
    let rows = stmt.query_map(params![timeline_id], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;

    let mut groups: Vec<TimelineUrlGroup> = Vec::new();
    for row in rows {
        let (url, win_title, webpage_title, ts) = row?;
        let url = url.trim().to_string();
        if url.is_empty() {
            continue;
        }
        let identity = canonical_document_identity(&url).unwrap_or_else(|| url.to_lowercase());
        let affinity = source_title_affinity(win_title.as_deref(), preferred_titles).max(
            source_title_affinity(webpage_title.as_deref(), preferred_titles),
        );
        let is_document = is_document_url(&url);

        if let Some(group) = groups.iter_mut().find(|group| group.identity == identity) {
            group.title_affinity = group.title_affinity.max(affinity);
            group.occurrence_count += 1;
            group.last_seen_ts = group.last_seen_ts.max(ts);
            group.is_document |= is_document;
            if prefer_representative_url(&group.representative_url, &url) {
                group.representative_url = url;
            }
        } else {
            groups.push(TimelineUrlGroup {
                identity,
                representative_url: url,
                title_affinity: affinity,
                occurrence_count: 1,
                last_seen_ts: ts,
                is_document,
            });
        }
    }

    if let Some(group) = groups
        .iter()
        .filter(|group| group.title_affinity > 0)
        .max_by_key(|group| {
            (
                group.title_affinity,
                group.is_document,
                group.occurrence_count,
                group.last_seen_ts,
            )
        })
    {
        return Ok(Some(group.representative_url.clone()));
    }

    let document_groups = groups
        .iter()
        .filter(|group| {
            group.is_document && !is_generic_document_landing_url(&group.representative_url)
        })
        .collect::<Vec<_>>();
    if document_groups.len() == 1 {
        return Ok(Some(document_groups[0].representative_url.clone()));
    }

    Ok(None)
}

impl StorageManager {
    pub fn get_document_templates(
        &self,
        limit: Option<usize>,
    ) -> Result<Vec<BakeDocumentRecord>, StorageError> {
        self.with_conn(|conn| {
            let lim = limit.unwrap_or(10);
            let mut stmt = conn.prepare(
                "SELECT id, title, doc_type, status, tags, applicable_tasks, source_memory_ids,
                        source_capture_ids, source_episode_ids, linked_knowledge_ids,
                        sections_json, style_phrases, replacement_rules, summary, full_content,
                        structured_content, prompt_hint, diagram_code, image_assets,
                        source_app_name, source_win_title, source_url, content_hash, language,
                        usage_count, match_score, match_level, creation_mode, review_status,
                        evidence_summary, generation_version, deleted_at,
                        created_at, updated_at
                 FROM bake_documents
                 WHERE deleted_at IS NULL AND status IN ('active', 'enabled')
                 ORDER BY COALESCE(match_score, 0) DESC, updated_at DESC
                 LIMIT ?1",
            )?;
            let rows = stmt.query_map([lim], |row| {
                Ok(BakeDocumentRecord {
                    id: row.get(0)?,
                    title: row.get(1)?,
                    doc_type: row.get(2)?,
                    status: row.get(3)?,
                    tags: row.get(4)?,
                    applicable_tasks: row.get(5)?,
                    source_memory_ids: row.get(6)?,
                    source_capture_ids: row.get(7)?,
                    source_episode_ids: row.get(8)?,
                    linked_knowledge_ids: row.get(9)?,
                    sections_json: row.get(10)?,
                    style_phrases: row.get(11)?,
                    replacement_rules: row.get(12)?,
                    summary: row.get(13)?,
                    full_content: row.get(14)?,
                    structured_content: row.get(15)?,
                    prompt_hint: row.get(16)?,
                    diagram_code: row.get(17)?,
                    image_assets: row.get(18)?,
                    source_app_name: row.get(19)?,
                    source_win_title: row.get(20)?,
                    source_url: row.get(21)?,
                    content_hash: row.get(22)?,
                    language: row.get(23)?,
                    usage_count: row.get(24)?,
                    match_score: row.get(25)?,
                    match_level: row.get(26)?,
                    creation_mode: row.get(27)?,
                    review_status: row.get(28)?,
                    evidence_summary: row.get(29)?,
                    generation_version: row.get(30)?,
                    deleted_at: row.get(31)?,
                    created_at: row.get(32)?,
                    updated_at: row.get(33)?,
                })
            })?;
            rows.collect::<Result<Vec<_>, _>>().map_err(Into::into)
        })
    }

    pub fn insert_timeline_entry(&self, entry: &NewTimeline) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            match entry.category.as_str() {
                "bake_knowledge" | "bake_sop" => {
                    let source = NewEpisodicMemory {
                        capture_id: entry.capture_id,
                        summary: entry.summary.clone(),
                        overview: entry.overview.clone(),
                        details: entry.details.clone(),
                        entities: entry.entities.clone(),
                        category: "bake_article".to_string(),
                        importance: entry.importance,
                        occurrence_count: entry.occurrence_count,
                        observed_at: entry.observed_at,
                        event_time_start: entry.event_time_start,
                        event_time_end: entry.event_time_end,
                        history_view: entry.history_view,
                        content_origin: entry.content_origin.clone(),
                        activity_type: entry.activity_type.clone(),
                        is_self_generated: entry.is_self_generated,
                        evidence_strength: entry.evidence_strength.clone(),
                        capture_ids: None,
                        start_time: None,
                        end_time: None,
                        duration_minutes: None,
                        frag_app_name: None,
                        frag_win_title: None,
                        time_range_start: None,
                        time_range_end: None,
                        key_timestamps: None,
                        work_item: entry.work_item.clone(),
                        work_status: entry.work_status.clone(),
                        work_progress: entry.work_progress.clone(),
                    };
                    let source_id = insert_episodic_memory_inner(conn, &source)?;
                    let now = current_ts_ms();
                    let title = entry.overview.as_deref().unwrap_or(&entry.summary);
                    let sql = if entry.category == "bake_knowledge" {
                        "INSERT INTO bake_knowledge (
                            timeline_id, title, summary, content, detailed_content, entities, importance,
                            user_verified, user_edited,
                            created_at, updated_at, created_at_ms, updated_at_ms
                         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 1, 0,
                                   datetime(?8 / 1000, 'unixepoch'), datetime(?8 / 1000, 'unixepoch'), ?8, ?8)"
                    } else {
                        "INSERT INTO bake_sops (
                            timeline_id, title, summary, content, detailed_content, entities, importance,
                            user_verified, user_edited,
                            created_at, updated_at, created_at_ms, updated_at_ms
                         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 1, 0,
                                   datetime(?8 / 1000, 'unixepoch'), datetime(?8 / 1000, 'unixepoch'), ?8, ?8)"
                    };
                    conn.execute(
                        sql,
                        params![
                            source_id,
                            title,
                            entry.summary,
                            entry.details,
                            entry.details,
                            entry.entities,
                            entry.importance,
                            now,
                        ],
                    )?;
                    Ok(conn.last_insert_rowid())
                }
                _ => insert_episodic_memory_inner(conn, entry),
            }
        })
    }

    /// 向后兼容函数：根据 category 查询对应的表
    pub fn list_timelines_by_category(
        &self,
        category: &str,
    ) -> Result<Vec<TimelineRecord>, StorageError> {
        match category {
            "bake_article" => {
                let memories = self.list_timelines_paginated(Some("bake_article"), 5000, 0)?;
                Ok(memories
                    .into_iter()
                    .map(|m| TimelineRecord {
                        id: m.id,
                        capture_id: m.capture_id,
                        summary: m.summary,
                        overview: m.overview,
                        details: m.details,
                        detailed_content: m.detailed_content,
                        entities: m.entities,
                        category: m.category,
                        importance: m.importance,
                        occurrence_count: m.occurrence_count,
                        observed_at: m.observed_at,
                        event_time_start: m.event_time_start,
                        event_time_end: m.event_time_end,
                        history_view: m.history_view,
                        content_origin: m.content_origin,
                        activity_type: m.activity_type,
                        is_self_generated: m.is_self_generated,
                        evidence_strength: m.evidence_strength,
                        user_verified: m.user_verified,
                        user_edited: m.user_edited,
                        created_at: m.created_at,
                        updated_at: m.updated_at,
                        created_at_ms: m.created_at_ms,
                        updated_at_ms: m.updated_at_ms,
                        capture_ids: None,
                        start_time: None,
                        end_time: None,
                        duration_minutes: None,
                        frag_app_name: None,
                        frag_win_title: None,
                        time_range_start: None,
                        time_range_end: None,
                        key_timestamps: None,
                    })
                    .collect())
            }
            "bake_knowledge" => {
                let knowledge = self.list_bake_knowledge_new(None, 5000, 0)?;
                Ok(knowledge
                    .into_iter()
                    .map(|k| TimelineRecord {
                        id: k.id,
                        capture_id: k.timeline_id,
                        summary: k.summary,
                        overview: Some(k.title),
                        details: k.content,
                        detailed_content: k.detailed_content,
                        entities: k.entities,
                        category: "bake_knowledge".to_string(),
                        importance: k.importance,
                        occurrence_count: None,
                        observed_at: None,
                        event_time_start: None,
                        event_time_end: None,
                        history_view: false,
                        content_origin: None,
                        activity_type: None,
                        is_self_generated: false,
                        evidence_strength: None,
                        user_verified: k.user_verified,
                        user_edited: k.user_edited,
                        created_at: k.created_at,
                        updated_at: k.updated_at,
                        created_at_ms: k.created_at_ms,
                        updated_at_ms: k.updated_at_ms,
                        capture_ids: None,
                        start_time: None,
                        end_time: None,
                        duration_minutes: None,
                        frag_app_name: None,
                        frag_win_title: None,
                        time_range_start: None,
                        time_range_end: None,
                        key_timestamps: None,
                    })
                    .collect())
            }
            "bake_sop" => {
                let sops = self.list_bake_sops_paginated(5000, 0)?;
                Ok(sops
                    .into_iter()
                    .map(|s| TimelineRecord {
                        id: s.id,
                        capture_id: s.timeline_id,
                        summary: s.summary,
                        overview: Some(s.title),
                        details: s.content,
                        detailed_content: s.detailed_content,
                        entities: s.entities,
                        category: "bake_sop".to_string(),
                        importance: s.importance,
                        occurrence_count: None,
                        observed_at: None,
                        event_time_start: None,
                        event_time_end: None,
                        history_view: false,
                        content_origin: None,
                        activity_type: None,
                        is_self_generated: false,
                        evidence_strength: None,
                        user_verified: s.user_verified,
                        user_edited: s.user_edited,
                        created_at: s.created_at,
                        updated_at: s.updated_at,
                        created_at_ms: s.created_at_ms,
                        updated_at_ms: s.updated_at_ms,
                        capture_ids: None,
                        start_time: None,
                        end_time: None,
                        duration_minutes: None,
                        frag_app_name: None,
                        frag_win_title: None,
                        time_range_start: None,
                        time_range_end: None,
                        key_timestamps: None,
                    })
                    .collect())
            }
            _ => {
                // 查询 timelines 表
                let memories = self.list_timelines_paginated(Some(category), 5000, 0)?;
                Ok(memories
                    .into_iter()
                    .map(|m| TimelineRecord {
                        id: m.id,
                        capture_id: m.capture_id,
                        summary: m.summary,
                        overview: m.overview,
                        details: m.details,
                        detailed_content: m.detailed_content,
                        entities: m.entities,
                        category: m.category,
                        importance: m.importance,
                        occurrence_count: m.occurrence_count,
                        observed_at: m.observed_at,
                        event_time_start: m.event_time_start,
                        event_time_end: m.event_time_end,
                        history_view: m.history_view,
                        content_origin: m.content_origin,
                        activity_type: m.activity_type,
                        is_self_generated: m.is_self_generated,
                        evidence_strength: m.evidence_strength,
                        user_verified: m.user_verified,
                        user_edited: m.user_edited,
                        created_at: m.created_at,
                        updated_at: m.updated_at,
                        created_at_ms: m.created_at_ms,
                        updated_at_ms: m.updated_at_ms,
                        capture_ids: None,
                        start_time: None,
                        end_time: None,
                        duration_minutes: None,
                        frag_app_name: None,
                        frag_win_title: None,
                        time_range_start: None,
                        time_range_end: None,
                        key_timestamps: None,
                    })
                    .collect())
            }
        }
    }

    pub fn list_bake_memories_paginated(
        &self,
        query: Option<&str>,
        from_ts: Option<i64>,
        to_ts: Option<i64>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<TimelineRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT k.id, k.capture_id, k.summary, k.overview, k.details, k.entities, k.category, k.importance,
                        k.occurrence_count, k.observed_at, k.event_time_start, k.event_time_end,
                        k.history_view, k.content_origin, k.activity_type, k.is_self_generated,
                        k.evidence_strength, k.user_verified, k.user_edited, k.created_at, k.updated_at,
                        k.created_at_ms, k.updated_at_ms, k.capture_ids, k.start_time, k.end_time, k.duration_minutes,
                        k.frag_app_name, k.frag_win_title, k.time_range_start, k.time_range_end, k.key_timestamps
                 FROM timelines k
                 WHERE 1 = 1",
            );
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = vec![];
            let query_terms = query.map(keyword_terms).unwrap_or_default();
            if !query_terms.is_empty() {
                let query_clause = query_terms
                    .iter()
                    .map(|_| {
                        "(k.summary LIKE ? OR COALESCE(k.overview, '') LIKE ? OR COALESCE(k.details, '') LIKE ? OR COALESCE(k.frag_win_title, '') LIKE ? OR EXISTS (
                            SELECT 1 FROM captures c
                            WHERE (c.id = k.capture_id OR COALESCE(k.capture_ids, '') LIKE ('%' || c.id || '%'))
                              AND (COALESCE(c.win_title, '') LIKE ? OR COALESCE(c.webpage_title, '') LIKE ? OR COALESCE(c.url, '') LIKE ? OR COALESCE(c.ax_text, '') LIKE ? OR COALESCE(c.ocr_text, '') LIKE ? OR COALESCE(c.input_text, '') LIKE ? OR COALESCE(c.audio_text, '') LIKE ?)
                        ))".to_string()
                    })
                    .collect::<Vec<_>>()
                    .join(" OR ");
                sql.push_str(" AND (");
                sql.push_str(&query_clause);
                sql.push(')');
                for term in &query_terms {
                    let pattern = format!("%{}%", term);
                    for _ in 0..11 {
                        bind_values.push(Box::new(pattern.clone()));
                    }
                }
                // FTS5 预筛：timelines_fts 命中候选可用时收窄扫描范围；
                // 候选为空/被截断/表缺失时自动回退原有 LIKE 全扫。
                if let Some(fts_query) = build_fts_or_query(&query_terms) {
                    if let Some(ids) =
                        fts_candidate_ids(conn, "timelines_fts", &fts_query, DEFAULT_FTS_CANDIDATE_CAP)
                    {
                        let (clause, mut id_binds) = render_in_clause(&ids);
                        sql.push_str(" AND k.id IN ");
                        sql.push_str(&clause);
                        bind_values.append(&mut id_binds);
                    }
                }
            }
            if let Some(value) = from_ts {
                sql.push_str(" AND k.created_at_ms >= ?");
                bind_values.push(Box::new(value));
            }
            if let Some(value) = to_ts {
                sql.push_str(" AND k.created_at_ms <= ?");
                bind_values.push(Box::new(value));
            }
            sql.push_str(" ORDER BY k.updated_at_ms DESC, k.id DESC LIMIT ? OFFSET ?");
            bind_values.push(Box::new(limit as i64));
            bind_values.push(Box::new(offset as i64));

            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> = bind_values.iter().map(|b| b.as_ref()).collect();
            let rows = stmt.query_map(params.as_slice(), |row| {
                Ok(row_to_timeline_entry(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;
            rows.collect::<Result<Vec<_>, _>>().map_err(StorageError::Sqlite)
        })
    }

    pub fn count_bake_memories_filtered(
        &self,
        query: Option<&str>,
        from_ts: Option<i64>,
        to_ts: Option<i64>,
    ) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT COUNT(*)
                 FROM timelines k
                 WHERE 1 = 1",
            );
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = vec![];
            let query_terms = query.map(keyword_terms).unwrap_or_default();
            if !query_terms.is_empty() {
                let query_clause = query_terms
                    .iter()
                    .map(|_| {
                        "(k.summary LIKE ? OR COALESCE(k.overview, '') LIKE ? OR COALESCE(k.details, '') LIKE ? OR COALESCE(k.frag_win_title, '') LIKE ? OR EXISTS (
                            SELECT 1 FROM captures c
                            WHERE (c.id = k.capture_id OR COALESCE(k.capture_ids, '') LIKE ('%' || c.id || '%'))
                              AND (COALESCE(c.win_title, '') LIKE ? OR COALESCE(c.webpage_title, '') LIKE ? OR COALESCE(c.url, '') LIKE ? OR COALESCE(c.ax_text, '') LIKE ? OR COALESCE(c.ocr_text, '') LIKE ? OR COALESCE(c.input_text, '') LIKE ? OR COALESCE(c.audio_text, '') LIKE ?)
                        ))".to_string()
                    })
                    .collect::<Vec<_>>()
                    .join(" OR ");
                sql.push_str(" AND (");
                sql.push_str(&query_clause);
                sql.push(')');
                for term in &query_terms {
                    let pattern = format!("%{}%", term);
                    for _ in 0..11 {
                        bind_values.push(Box::new(pattern.clone()));
                    }
                }
                // FTS5 预筛（与列表查询保持一致的候选收窄）
                if let Some(fts_query) = build_fts_or_query(&query_terms) {
                    if let Some(ids) =
                        fts_candidate_ids(conn, "timelines_fts", &fts_query, DEFAULT_FTS_CANDIDATE_CAP)
                    {
                        let (clause, mut id_binds) = render_in_clause(&ids);
                        sql.push_str(" AND k.id IN ");
                        sql.push_str(&clause);
                        bind_values.append(&mut id_binds);
                    }
                }
            }
            if let Some(value) = from_ts {
                sql.push_str(" AND k.created_at_ms >= ?");
                bind_values.push(Box::new(value));
            }
            if let Some(value) = to_ts {
                sql.push_str(" AND k.created_at_ms <= ?");
                bind_values.push(Box::new(value));
            }

            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> = bind_values.iter().map(|b| b.as_ref()).collect();
            stmt.query_row(params.as_slice(), |row| row.get(0)).map_err(StorageError::Sqlite)
        })
    }

    /// 基于 timelines_fts 预计算关键词搜索候选时间线 ID。
    ///
    /// 返回 `None` 表示 FTS 不可用或候选不可靠（为空/被截断），
    /// 调用方应回退为原有全量过滤路径。
    pub fn timeline_fts_candidate_ids(&self, query: &str) -> Option<Vec<i64>> {
        let terms = keyword_terms(query);
        let fts_query = build_fts_or_query(&terms)?;
        self.with_conn(|conn| {
            Ok(fts_candidate_ids(
                conn,
                "timelines_fts",
                &fts_query,
                DEFAULT_FTS_CANDIDATE_CAP,
            ))
        })
        .ok()
        .flatten()
    }

    /// 基于 bake_sops_fts 预计算关键词搜索候选 SOP ID（bake_sops 行 id）。
    ///
    /// SOP 列表的可搜索字段来自 bake_sops 表（title/summary/content），
    /// 因此预筛必须走 bake_sops_fts 而非 timelines_fts。
    /// 返回 `None` 表示 FTS 不可用或候选不可靠（为空/被截断），
    /// 调用方应回退为原有全量过滤路径。
    pub fn bake_sop_fts_candidate_ids(&self, query: &str) -> Option<Vec<i64>> {
        let terms = keyword_terms(query);
        let fts_query = build_fts_or_query(&terms)?;
        self.with_conn(|conn| {
            Ok(fts_candidate_ids(
                conn,
                "bake_sops_fts",
                &fts_query,
                DEFAULT_FTS_CANDIDATE_CAP,
            ))
        })
        .ok()
        .flatten()
    }

    pub fn list_bake_knowledge_paginated(
        &self,
        query: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<TimelineRecord>, StorageError> {
        // 使用新表，但返回旧格式以保持兼容
        let knowledge = self.list_bake_knowledge_new(query, limit, offset)?;
        Ok(knowledge
            .into_iter()
            .map(|k| TimelineRecord {
                id: k.id,
                capture_id: k.timeline_id,
                summary: k.summary,
                overview: Some(k.title),
                details: k.content,
                detailed_content: k.detailed_content,
                entities: k.entities,
                category: "bake_knowledge".to_string(),
                importance: k.importance,
                occurrence_count: Some(k.occurrence_count),
                observed_at: None,
                event_time_start: None,
                event_time_end: None,
                history_view: false,
                content_origin: None,
                activity_type: None,
                is_self_generated: false,
                evidence_strength: None,
                user_verified: k.user_verified,
                user_edited: k.user_edited,
                created_at: k.created_at,
                updated_at: k.updated_at,
                created_at_ms: k.created_at_ms,
                updated_at_ms: k.updated_at_ms,
                capture_ids: None,
                start_time: None,
                end_time: None,
                duration_minutes: None,
                frag_app_name: None,
                frag_win_title: None,
                time_range_start: None,
                time_range_end: None,
                key_timestamps: None,
            })
            .collect())
    }

    /// 新的 bake_knowledge 查询函数（返回新类型）
    fn list_bake_knowledge_new(
        &self,
        query: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<BakeKnowledgeRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT b.id, COALESCE(b.timeline_id, 0), b.title, b.summary, b.content, b.detailed_content, b.entities, b.importance,
                        b.user_verified, b.user_edited, b.created_at, b.updated_at, b.created_at_ms, b.updated_at_ms,
                        b.source_capture_ids,
                        COALESCE((
                            SELECT SUM(COALESCE(t.occurrence_count, 1))
                            FROM bake_artifact_source_links l
                            JOIN timelines t ON t.id = l.source_timeline_id
                            WHERE l.artifact_kind = 'knowledge' AND l.artifact_id = b.id
                        ), (
                            SELECT COALESCE(t.occurrence_count, 1)
                            FROM timelines t WHERE t.id = b.timeline_id
                        ), 1) AS occurrence_count
                 FROM bake_knowledge b",
            );
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
            if let Some(value) = query.map(str::trim).filter(|value| !value.is_empty()) {
                sql.push_str(
                    " WHERE (b.summary LIKE ? OR b.title LIKE ? OR COALESCE(b.content, '') LIKE ?
                              OR COALESCE(b.detailed_content, '') LIKE ? OR COALESCE(b.entities, '') LIKE ?)",
                );
                let pattern = format!("%{}%", value);
                for _ in 0..5 {
                    bind_values.push(Box::new(pattern.clone()));
                }
            }
            sql.push_str(" ORDER BY b.updated_at_ms DESC LIMIT ? OFFSET ?");
            bind_values.push(Box::new(limit as i64));
            bind_values.push(Box::new(offset as i64));

            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> =
                bind_values.iter().map(|value| value.as_ref()).collect();
            let rows = stmt.query_map(params.as_slice(), |row| {
                Ok(row_to_bake_knowledge(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;
            rows.collect::<Result<Vec<_>, _>>().map_err(StorageError::Sqlite)
        })
    }

    pub fn count_bake_knowledge_filtered(&self, query: Option<&str>) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from("SELECT COUNT(*) FROM bake_knowledge");
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
            if let Some(q) = query {
                sql.push_str(" WHERE (summary LIKE ? OR COALESCE(title, '') LIKE ? OR COALESCE(content, '') LIKE ? OR COALESCE(entities, '') LIKE ?)");
                let pattern = format!("%{}%", q);
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern));
            }

            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> = bind_values.iter().map(|b| b.as_ref()).collect();
            stmt.query_row(params.as_slice(), |row| row.get(0)).map_err(StorageError::Sqlite)
        })
    }

    pub fn list_non_bake_knowledge_paginated(
        &self,
        query: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<TimelineRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT id, capture_id, summary, overview, details, entities, category, importance,
                        occurrence_count, observed_at, event_time_start, event_time_end,
                        history_view, content_origin, activity_type, is_self_generated,
                        evidence_strength, user_verified, user_edited, created_at, updated_at,
                        created_at_ms, updated_at_ms, capture_ids, start_time, end_time,
                        duration_minutes, frag_app_name, frag_win_title, time_range_start,
                        time_range_end, key_timestamps
                 FROM timelines
                 WHERE category NOT IN (?, ?, ?, ?)",
            );
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = vec![
                Box::new("bake_article".to_string()),
                Box::new("bake_sop".to_string()),
                Box::new("bake_knowledge".to_string()),
                Box::new("legacy_bake_candidate".to_string()),
            ];
            if let Some(q) = query {
                sql.push_str(" AND (summary LIKE ? OR COALESCE(overview, '') LIKE ? OR COALESCE(details, '') LIKE ? OR COALESCE(category, '') LIKE ?)");
                let pattern = format!("%{}%", q);
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern));
            }
            sql.push_str(" ORDER BY updated_at_ms DESC, id DESC LIMIT ? OFFSET ?");
            bind_values.push(Box::new(limit as i64));
            bind_values.push(Box::new(offset as i64));

            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> = bind_values.iter().map(|b| b.as_ref()).collect();
            let rows = stmt.query_map(params.as_slice(), |row| {
                Ok(row_to_timeline_entry(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;
            rows.collect::<Result<Vec<_>, _>>().map_err(StorageError::Sqlite)
        })
    }

    pub fn count_non_bake_knowledge_filtered(
        &self,
        query: Option<&str>,
    ) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT COUNT(*) FROM timelines WHERE category NOT IN (?, ?, ?, ?)",
            );
            let mut bind_values: Vec<Box<dyn rusqlite::ToSql>> = vec![
                Box::new("bake_article".to_string()),
                Box::new("bake_sop".to_string()),
                Box::new("bake_knowledge".to_string()),
                Box::new("legacy_bake_candidate".to_string()),
            ];
            if let Some(q) = query {
                sql.push_str(" AND (summary LIKE ? OR COALESCE(overview, '') LIKE ? OR COALESCE(details, '') LIKE ? OR COALESCE(category, '') LIKE ?)");
                let pattern = format!("%{}%", q);
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern.clone()));
                bind_values.push(Box::new(pattern));
            }

            let mut stmt = conn.prepare(&sql)?;
            let params: Vec<&dyn rusqlite::ToSql> = bind_values.iter().map(|b| b.as_ref()).collect();
            stmt.query_row(params.as_slice(), |row| row.get(0)).map_err(StorageError::Sqlite)
        })
    }

    pub fn list_non_bake_knowledge(
        &self,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<TimelineRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, capture_id, summary, overview, details, entities, category, importance,
                        occurrence_count, observed_at, event_time_start, event_time_end,
                        history_view, content_origin, activity_type, is_self_generated,
                        evidence_strength, user_verified, user_edited, created_at, updated_at,
                        created_at_ms, updated_at_ms, capture_ids, start_time, end_time,
                        duration_minutes, frag_app_name, frag_win_title, time_range_start,
                        time_range_end, key_timestamps
                 FROM timelines
                 WHERE category NOT IN (?1, ?2, ?3, ?4)
                 ORDER BY updated_at_ms DESC, id DESC
                 LIMIT ?5 OFFSET ?6",
            )?;
            let rows = stmt.query_map(
                params![
                    "bake_article",
                    "bake_sop",
                    "bake_knowledge",
                    "legacy_bake_candidate",
                    limit as i64,
                    offset as i64
                ],
                |row| Ok(row_to_timeline_entry(row).map_err(|_| rusqlite::Error::InvalidQuery)?),
            )?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    pub fn count_non_bake_knowledge(&self) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT COUNT(*) FROM timelines WHERE category NOT IN (?1, ?2, ?3, ?4)",
                params![
                    "bake_article",
                    "bake_sop",
                    "bake_knowledge",
                    "legacy_bake_candidate"
                ],
                |row| row.get(0),
            )
            .map_err(StorageError::Sqlite)
        })
    }

    pub fn list_bake_memory_init_candidates(
        &self,
        since_ts_ms: i64,
        limit: usize,
    ) -> Result<Vec<BakeMemorySourceRecord>, StorageError> {
        self.list_bake_memory_init_candidates_with_max_failures(since_ts_ms, limit, i64::MAX)
    }

    /// 与 [`list_bake_memory_init_candidates`] 相同，但额外按 `bake_retry_state.failure_count`
    /// 过滤：失败次数 >= `max_failures` 的 timeline 会进入终态，避免毒丸候选反复触发整轮失败。
    pub fn list_bake_memory_init_candidates_with_max_failures(
        &self,
        since_ts_ms: i64,
        limit: usize,
        max_failures: i64,
    ) -> Result<Vec<BakeMemorySourceRecord>, StorageError> {
        let mut records =
            self.list_bake_memory_fresh_candidates(since_ts_ms, limit, max_failures)?;
        records.extend(self.list_bake_memory_retry_candidates(limit, max_failures)?);
        records.sort_by_key(|candidate| (candidate.timeline.updated_at_ms, candidate.timeline.id));
        records.truncate(limit);
        Ok(records)
    }

    /// 新候选只受 unified watermark 控制，不与历史重试共享扫描窗口。
    pub fn list_bake_memory_fresh_candidates(
        &self,
        since_ts_ms: i64,
        limit: usize,
        max_failures: i64,
    ) -> Result<Vec<BakeMemorySourceRecord>, StorageError> {
        self.list_bake_memory_candidates_by_lane(since_ts_ms, limit, max_failures, false)
    }

    /// 重试候选独立于 watermark，并且只有到达持久化调度时间且尚无任何产物时
    /// 才能进入执行队列。
    pub fn list_bake_memory_retry_candidates(
        &self,
        limit: usize,
        max_failures: i64,
    ) -> Result<Vec<BakeMemorySourceRecord>, StorageError> {
        self.list_bake_memory_candidates_by_lane(0, limit, max_failures, true)
    }

    fn list_bake_memory_candidates_by_lane(
        &self,
        since_ts_ms: i64,
        limit: usize,
        max_failures: i64,
        retry_lane: bool,
    ) -> Result<Vec<BakeMemorySourceRecord>, StorageError> {
        let lane_predicate = if retry_lane {
            r#"
                COALESCE(r.failure_count, 0) > 0
                AND r.failure_count < ?3
                AND COALESCE(r.next_retry_at_ms, 0) <= ?4
                AND NOT EXISTS (SELECT 1 FROM bake_knowledge bk WHERE bk.timeline_id = k.id)
                AND NOT EXISTS (SELECT 1 FROM bake_sops bs WHERE bs.timeline_id = k.id)
                AND NOT EXISTS (
                    SELECT 1
                    FROM bake_documents bd
                    WHERE bd.deleted_at IS NULL
                      AND (
                           (json_valid(COALESCE(bd.source_memory_ids, '[]')) AND EXISTS (
                               SELECT 1 FROM json_each(bd.source_memory_ids)
                               WHERE CAST(json_each.value AS TEXT) = CAST(k.id AS TEXT)
                           ))
                        OR (json_valid(COALESCE(bd.source_episode_ids, '[]')) AND EXISTS (
                               SELECT 1 FROM json_each(bd.source_episode_ids)
                               WHERE CAST(json_each.value AS TEXT) = CAST(k.id AS TEXT)
                           ))
                      )
                )
            "#
        } else {
            r#"
                COALESCE(r.failure_count, 0) = 0
                AND ?3 > 0
                AND ?4 >= 0
                AND (
                    MAX(k.updated_at_ms, COALESCE((SELECT MAX(c2.ts) FROM captures c2 WHERE c2.timeline_id = k.id), 0)) > ?1
                    OR EXISTS (
                        SELECT 1
                        FROM bake_documents d
                        JOIN captures c3 ON c3.timeline_id = k.id
                        WHERE d.deleted_at IS NULL
                          AND json_valid(COALESCE(d.source_memory_ids, '[]'))
                          AND EXISTS (
                              SELECT 1 FROM json_each(d.source_memory_ids)
                              WHERE CAST(json_each.value AS TEXT) = CAST(k.id AS TEXT)
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM json_each(
                                  CASE
                                      WHEN json_valid(COALESCE(d.source_capture_ids, '[]'))
                                      THEN d.source_capture_ids
                                      ELSE '[]'
                                  END
                              )
                              WHERE CAST(json_each.value AS TEXT) = CAST(c3.id AS TEXT)
                          )
                    )
                )
            "#
        };
        self.with_conn(|conn| {
            let sql = format!(
                "SELECT k.id, k.capture_id, k.summary, k.overview, k.details, k.entities, k.category, k.importance,
                        k.occurrence_count, k.observed_at, k.event_time_start, k.event_time_end,
                        k.history_view, k.content_origin, k.activity_type, k.is_self_generated,
                        k.evidence_strength, k.user_verified, k.user_edited, k.created_at, k.updated_at,
                        k.created_at_ms,
                        MAX(k.updated_at_ms, COALESCE((SELECT MAX(c2.ts) FROM captures c2 WHERE c2.timeline_id = k.id), 0)),
                        k.capture_ids, k.start_time, k.end_time,
                        k.duration_minutes, k.frag_app_name, k.frag_win_title, k.time_range_start,
                        k.time_range_end, k.key_timestamps,
                        c.ts, c.app_name, c.win_title, c.ax_text, c.ocr_text, c.input_text, c.audio_text,
                        c.url, c.webpage_title,
                        COALESCE(r.failure_count, 0), r.last_error_code,
                        COALESCE(r.next_retry_at_ms, 0),
                        k.work_item, k.work_status, k.work_progress
                 FROM timelines k
                 INNER JOIN captures c ON c.id = k.capture_id
                 LEFT JOIN bake_retry_state r ON r.timeline_id = k.id
                 WHERE k.category NOT IN ('bake_article', 'bake_knowledge', 'bake_sop', 'legacy_bake_candidate')
                   AND ({lane_predicate})
                 ORDER BY MAX(k.updated_at_ms, COALESCE((SELECT MAX(c2.ts) FROM captures c2 WHERE c2.timeline_id = k.id), 0)) ASC, k.id ASC
                 LIMIT ?2"
            );
            let mut stmt = conn.prepare(&sql)?;
            let rows = stmt.query_map(
                params![since_ts_ms, limit as i64, max_failures, current_ts_ms()],
                |row| {
                Ok(BakeMemorySourceRecord {
                    timeline: row_to_timeline_record(row).map_err(|_| rusqlite::Error::InvalidQuery)?,
                    capture_ts: row.get(32)?,
                    capture_app_name: row.get(33)?,
                    capture_win_title: row.get(34)?,
                    capture_ax_text: row.get(35)?,
                    capture_ocr_text: row.get(36)?,
                    capture_input_text: row.get(37)?,
                    capture_audio_text: row.get(38)?,
                    capture_url: row.get::<_, Option<String>>(39)?.and_then(|s| {
                        let t = s.trim();
                        if t.is_empty() { None } else { Some(t.to_string()) }
                    }),
                    capture_webpage_title: row.get(40)?,
                    preferred_source_title: None,
                    url_aggregated_text: None,
                    url_aggregated_capture_count: 0,
                    action_trace: Vec::new(),
                    work_item: row.get(44)?,
                    work_status: row.get(45)?,
                    work_progress: row.get(46)?,
                    retry_failure_count: row.get(41)?,
                    retry_error_code: row.get(42)?,
                    retry_next_at_ms: row.get(43)?,
                })
            })?;
            let mut records: Vec<BakeMemorySourceRecord> =
                rows.collect::<Result<Vec<_>, _>>().map_err(StorageError::Sqlite)?;
            for record in records.iter_mut() {
                let full_member_ids =
                    list_timeline_capture_ids(conn, record.timeline.id, record.timeline.capture_id)?;
                if !full_member_ids.is_empty() {
                    record.timeline.capture_ids = Some(to_json_array_string(&full_member_ids));
                }
                record.preferred_source_title = preferred_member_source_title(
                    conn,
                    record.timeline.id,
                    record.timeline.capture_id,
                )?;

                if record.capture_url.is_none() {
                    let preferred_titles = [
                        record.capture_webpage_title.as_deref(),
                        record.capture_win_title.as_deref(),
                        record.timeline.frag_win_title.as_deref(),
                    ]
                    .into_iter()
                    .flatten()
                    .collect::<Vec<_>>();
                    record.capture_url = find_timeline_fallback_source_url(
                        conn,
                        record.timeline.id,
                        &preferred_titles,
                    )?;
                }

                // 优先聚合 timeline 全部成员 capture 的内容（含文档型成员的正文），
                // 因为主 capture 常是 IM，代表不了 timeline 里浏览/编辑的文档。
                let member_ids = parse_capture_ids(record.timeline.capture_ids.as_deref());
                record.action_trace = load_member_action_trace(conn, &member_ids)?;
                let member_aggregated = if member_ids.len() > 1 {
                    aggregate_member_capture_text(
                        conn,
                        &member_ids,
                        record.timeline.capture_id,
                    )?
                } else {
                    None
                };

                if let Some((aggregated, count)) = member_aggregated {
                    record.url_aggregated_text = Some(aggregated);
                    record.url_aggregated_capture_count = count;
                } else if let Some(url) = record.capture_url.clone() {
                    // 回退：单 capture 场景仍按主 capture 的 URL 聚合历史浏览。
                    if let Some((aggregated, count)) =
                        aggregate_url_capture_text(conn, &url, record.capture_ts)?
                    {
                        record.url_aggregated_text = Some(aggregated);
                        record.url_aggregated_capture_count = count;
                    }
                }
            }
            Ok(records)
        })
    }

    pub fn get_timeline_entry(&self, id: i64) -> Result<Option<TimelineRecord>, StorageError> {
        if let Some(knowledge) = self.get_bake_knowledge(id)? {
            return Ok(Some(TimelineRecord {
                id: knowledge.id,
                capture_id: knowledge.timeline_id,
                summary: knowledge.summary,
                overview: Some(knowledge.title),
                details: knowledge.content,
                detailed_content: knowledge.detailed_content,
                entities: knowledge.entities,
                category: "bake_knowledge".to_string(),
                importance: knowledge.importance,
                occurrence_count: None,
                observed_at: None,
                event_time_start: None,
                event_time_end: None,
                history_view: false,
                content_origin: None,
                activity_type: None,
                is_self_generated: false,
                evidence_strength: None,
                user_verified: knowledge.user_verified,
                user_edited: knowledge.user_edited,
                created_at: knowledge.created_at,
                updated_at: knowledge.updated_at,
                created_at_ms: knowledge.created_at_ms,
                updated_at_ms: knowledge.updated_at_ms,
                capture_ids: None,
                start_time: None,
                end_time: None,
                duration_minutes: None,
                frag_app_name: None,
                frag_win_title: None,
                time_range_start: None,
                time_range_end: None,
                key_timestamps: None,
            }));
        }

        if let Some(sop) = self.get_bake_sop(id)? {
            return Ok(Some(TimelineRecord {
                id: sop.id,
                capture_id: sop.timeline_id,
                summary: sop.summary,
                overview: Some(sop.title),
                details: sop.content,
                detailed_content: sop.detailed_content,
                entities: sop.entities,
                category: "bake_sop".to_string(),
                importance: sop.importance,
                occurrence_count: None,
                observed_at: None,
                event_time_start: None,
                event_time_end: None,
                history_view: false,
                content_origin: None,
                activity_type: None,
                is_self_generated: false,
                evidence_strength: None,
                user_verified: sop.user_verified,
                user_edited: sop.user_edited,
                created_at: sop.created_at,
                updated_at: sop.updated_at,
                created_at_ms: sop.created_at_ms,
                updated_at_ms: sop.updated_at_ms,
                capture_ids: None,
                start_time: None,
                end_time: None,
                duration_minutes: None,
                frag_app_name: None,
                frag_win_title: None,
                time_range_start: None,
                time_range_end: None,
                key_timestamps: None,
            }));
        }

        if let Some(memory) = self.get_episodic_memory(id)? {
            return Ok(Some(TimelineRecord {
                id: memory.id,
                capture_id: memory.capture_id,
                summary: memory.summary,
                overview: memory.overview,
                details: memory.details,
                detailed_content: memory.detailed_content,
                entities: memory.entities,
                category: memory.category,
                importance: memory.importance,
                occurrence_count: memory.occurrence_count,
                observed_at: memory.observed_at,
                event_time_start: memory.event_time_start,
                event_time_end: memory.event_time_end,
                history_view: memory.history_view,
                content_origin: memory.content_origin,
                activity_type: memory.activity_type,
                is_self_generated: memory.is_self_generated,
                evidence_strength: memory.evidence_strength,
                user_verified: memory.user_verified,
                user_edited: memory.user_edited,
                created_at: memory.created_at,
                updated_at: memory.updated_at,
                created_at_ms: memory.created_at_ms,
                updated_at_ms: memory.updated_at_ms,
                capture_ids: None,
                start_time: None,
                end_time: None,
                duration_minutes: None,
                frag_app_name: None,
                frag_win_title: None,
                time_range_start: None,
                time_range_end: None,
                key_timestamps: None,
            }));
        }

        Ok(None)
    }

    pub fn update_timeline_details(
        &self,
        id: i64,
        summary: &str,
        overview: Option<&str>,
        details: Option<&str>,
        entities: &str,
    ) -> Result<bool, StorageError> {
        let Some(entry) = self.get_timeline_entry(id)? else {
            return Ok(false);
        };

        match entry.category.as_str() {
            "bake_article" => self.update_episodic_memory(id, summary, overview, details, entities),
            "bake_knowledge" => {
                let title = overview.or(entry.overview.as_deref()).unwrap_or(summary);
                self.update_bake_knowledge(id, title, summary, details, entities)
            }
            "bake_sop" => {
                let title = overview.or(entry.overview.as_deref()).unwrap_or(summary);
                self.update_bake_sop(id, title, summary, details, entities)
            }
            _ => self.update_episodic_memory(id, summary, overview, details, entities),
        }
    }

    pub fn update_timeline_details_system(
        &self,
        id: i64,
        summary: &str,
        overview: Option<&str>,
        details: Option<&str>,
        entities: &str,
    ) -> Result<bool, StorageError> {
        let Some(entry) = self.get_timeline_entry(id)? else {
            return Ok(false);
        };

        self.with_conn(|conn| {
            let now = current_ts_ms();
            let title = overview.or(entry.overview.as_deref()).unwrap_or(summary);
            let affected = match entry.category.as_str() {
                "bake_article" => conn.execute(
                    "UPDATE timelines
                     SET summary = ?1, overview = ?2, details = ?3, entities = ?4,
                         updated_at = datetime(?6 / 1000, 'unixepoch'), updated_at_ms = ?6
                     WHERE id = ?5",
                    params![summary, overview, details, entities, id, now],
                )?,
                "bake_knowledge" => conn.execute(
                    "UPDATE bake_knowledge
                     SET title = ?1, summary = ?2, content = ?3, entities = ?4,
                         updated_at = datetime(?6 / 1000, 'unixepoch'), updated_at_ms = ?6
                     WHERE id = ?5",
                    params![title, summary, details, entities, id, now],
                )?,
                "bake_sop" => conn.execute(
                    "UPDATE bake_sops
                     SET title = ?1, summary = ?2, content = ?3, entities = ?4,
                         updated_at = datetime(?6 / 1000, 'unixepoch'), updated_at_ms = ?6
                     WHERE id = ?5",
                    params![title, summary, details, entities, id, now],
                )?,
                _ => conn.execute(
                    "UPDATE timelines
                     SET summary = ?1, overview = ?2, details = ?3, entities = ?4,
                         updated_at = datetime(?6 / 1000, 'unixepoch'), updated_at_ms = ?6
                     WHERE id = ?5",
                    params![summary, overview, details, entities, id, now],
                )?,
            };
            Ok(affected > 0)
        })
    }

    pub fn set_knowledge_verified(&self, id: i64, verified: bool) -> Result<bool, StorageError> {
        let Some(entry) = self.get_timeline_entry(id)? else {
            return Ok(false);
        };

        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = match entry.category.as_str() {
                "bake_article" => conn.execute(
                    "UPDATE timelines SET user_verified = ?1,
                     updated_at = datetime(?3 / 1000, 'unixepoch'), updated_at_ms = ?3
                     WHERE id = ?2",
                    params![verified, id, now],
                )?,
                "bake_knowledge" => conn.execute(
                    "UPDATE bake_knowledge SET user_verified = ?1,
                     updated_at = datetime(?3 / 1000, 'unixepoch'), updated_at_ms = ?3
                     WHERE id = ?2",
                    params![verified, id, now],
                )?,
                "bake_sop" => conn.execute(
                    "UPDATE bake_sops SET user_verified = ?1,
                     updated_at = datetime(?3 / 1000, 'unixepoch'), updated_at_ms = ?3
                     WHERE id = ?2",
                    params![verified, id, now],
                )?,
                _ => conn.execute(
                    "UPDATE timelines SET user_verified = ?1,
                     updated_at = datetime(?3 / 1000, 'unixepoch'), updated_at_ms = ?3
                     WHERE id = ?2",
                    params![verified, id, now],
                )?,
            };
            Ok(affected > 0)
        })
    }

    pub fn delete_knowledge_entry(&self, id: i64) -> Result<bool, StorageError> {
        let Some(entry) = self.get_timeline_entry(id)? else {
            return Ok(false);
        };

        match entry.category.as_str() {
            "bake_article" => self.delete_episodic_memory(id),
            "bake_knowledge" => self.delete_bake_knowledge(id),
            "bake_sop" => self.delete_bake_sop(id),
            _ => self.delete_episodic_memory(id),
        }
    }
}

const URL_AGGREGATION_LOOKBACK_MS: i64 = 30 * 24 * 3600 * 1000;
const URL_AGGREGATION_MAX_CAPTURES: i64 = 30;
const URL_AGGREGATION_TOTAL_BUDGET_CHARS: usize = 32_000;
const URL_AGGREGATION_PER_CAPTURE_CAP_CHARS: usize = 16_000;
const URL_AGGREGATION_DEDUP_HEAD_CHARS: usize = 200;

// 成员聚合：把一条 timeline 的 capture_ids 数组里所有成员的可见文本拼起来，
// 用于补充主 capture 之外的内容（尤其文档型成员，主 capture 常是 IM 无法代表）。
const MEMBER_AGGREGATION_MAX_CAPTURES: usize = 40;
const MEMBER_AGGREGATION_TOTAL_BUDGET_CHARS: usize = 32_000;
const MEMBER_AGGREGATION_PER_CAPTURE_CAP_CHARS: usize = 16_000;
const MEMBER_AGGREGATION_DEDUP_HEAD_CHARS: usize = 200;
const ACTION_TRACE_MAX_CAPTURES: usize = 40;
const ACTION_TRACE_VISIBLE_TEXT_CHARS: usize = 1_000;
const ACTION_TRACE_INPUT_TEXT_CHARS: usize = 400;
const ACTION_TRACE_AUDIO_TEXT_CHARS: usize = 400;
const ACTION_TRACE_FOCUSED_ROLE_CHARS: usize = 100;
const ACTION_TRACE_FOCUSED_ID_CHARS: usize = 240;
const ACTION_TRACE_STATE_DELTA_CHARS: usize = 320;

/// 构建操作提炼专用的严格时间序轨迹。
///
/// 与文档正文聚合不同，这里不按页面开头去重、不把文档帧提前，确保同一界面
/// 在操作前后的状态变化，以及 A -> B -> A 的跨应用顺序都能送达 sidecar。
fn load_member_action_trace(
    conn: &Connection,
    capture_ids: &[i64],
) -> Result<Vec<BakeActionTraceRecord>, StorageError> {
    if capture_ids.is_empty() {
        return Ok(Vec::new());
    }
    let placeholders = capture_ids
        .iter()
        .map(|_| "?")
        .collect::<Vec<_>>()
        .join(",");
    let sql = format!(
        "SELECT id, ts, event_type, app_name, win_title, url, webpage_title,
                ax_text, ocr_text, input_text, audio_text,
                ax_focused_role, ax_focused_id
         FROM captures
         WHERE id IN ({placeholders})
         ORDER BY ts ASC, id ASC
         LIMIT {ACTION_TRACE_MAX_CAPTURES}"
    );
    let params_vec: Vec<&dyn rusqlite::ToSql> = capture_ids
        .iter()
        .map(|id| id as &dyn rusqlite::ToSql)
        .collect();
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(params_vec.as_slice(), |row| {
        let ax_text = row.get::<_, Option<String>>(7)?;
        let ocr_text = row.get::<_, Option<String>>(8)?;
        let visible_text = combine_distinct_capture_text(ax_text.as_deref(), ocr_text.as_deref());
        Ok(BakeActionTraceRecord {
            capture_id: row.get(0)?,
            ts: row.get(1)?,
            event_type: row
                .get::<_, Option<String>>(2)?
                .unwrap_or_else(|| "auto".to_string()),
            app_name: truncate_optional_text(row.get(3)?, 100),
            win_title: truncate_optional_text(row.get(4)?, 180),
            url: truncate_optional_text(row.get(5)?, 500),
            webpage_title: truncate_optional_text(row.get(6)?, 180),
            visible_text: truncate_optional_text(visible_text, ACTION_TRACE_VISIBLE_TEXT_CHARS),
            input_text: truncate_optional_text(row.get(9)?, ACTION_TRACE_INPUT_TEXT_CHARS),
            audio_text: truncate_optional_text(row.get(10)?, ACTION_TRACE_AUDIO_TEXT_CHARS),
            ax_focused_role: truncate_optional_text(row.get(11)?, ACTION_TRACE_FOCUSED_ROLE_CHARS),
            ax_focused_id: truncate_optional_text(row.get(12)?, ACTION_TRACE_FOCUSED_ID_CHARS),
            state_delta: None,
            evidence_kind: None,
            operation_evidence: false,
        })
    })?;
    let records = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(StorageError::Sqlite)?;
    Ok(annotate_and_compact_action_trace(records))
}

fn annotate_and_compact_action_trace(
    records: Vec<BakeActionTraceRecord>,
) -> Vec<BakeActionTraceRecord> {
    let mut annotated = Vec::with_capacity(records.len());
    let mut previous: Option<BakeActionTraceRecord> = None;

    for mut record in records {
        let state_delta = previous
            .as_ref()
            .and_then(|item| build_action_state_delta(item, &record));
        let state_changed = state_delta.is_some();
        let has_input = record
            .input_text
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty());
        let has_focus = record.ax_focused_role.is_some() || record.ax_focused_id.is_some();
        let (evidence_kind, operation_evidence) = match record.event_type.as_str() {
            "mouse_click" => ("interaction", true),
            "browser_navigation" | "app_switch" => ("navigation", true),
            "key_pause" if has_input || has_focus || state_changed => ("input", true),
            "manual" if has_input || has_focus || state_changed => ("interaction", true),
            "auto" if state_changed => ("state_change", true),
            _ => ("context", false),
        };
        record.state_delta = state_delta;
        record.evidence_kind = Some(evidence_kind.to_string());
        record.operation_evidence = operation_evidence;
        previous = Some(record.clone());

        // 连续滚动只保留最后一帧；连续无变化的 auto/context 也只保留最后一帧。
        // 它们仍能给模型提供上下文，但不会膨胀有效操作证据数。
        let replace_previous_context =
            annotated
                .last()
                .is_some_and(|last: &BakeActionTraceRecord| {
                    !last.operation_evidence
                        && !record.operation_evidence
                        && ((last.event_type == "scroll" && record.event_type == "scroll")
                            || (last.evidence_kind.as_deref() == Some("context")
                                && record.evidence_kind.as_deref() == Some("context")
                                && record.state_delta.is_none()))
                });
        if replace_previous_context {
            annotated.pop();
        }
        annotated.push(record);
    }

    annotated
}

fn build_action_state_delta(
    previous: &BakeActionTraceRecord,
    current: &BakeActionTraceRecord,
) -> Option<String> {
    let mut parts = Vec::new();
    push_action_context_delta(&mut parts, "app", &previous.app_name, &current.app_name);
    push_action_context_delta(
        &mut parts,
        "window",
        &previous.win_title,
        &current.win_title,
    );
    push_action_context_delta(&mut parts, "url", &previous.url, &current.url);

    if materially_different_text(
        previous.visible_text.as_deref(),
        current.visible_text.as_deref(),
    ) {
        if let Some(current_text) = current.visible_text.as_deref() {
            let text = current_text
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ");
            if !text.is_empty() {
                parts.push(format!("visible→{text}"));
            }
        }
    }

    if parts.is_empty() {
        None
    } else {
        Some(
            parts
                .join("; ")
                .chars()
                .take(ACTION_TRACE_STATE_DELTA_CHARS)
                .collect(),
        )
    }
}

fn push_action_context_delta(
    parts: &mut Vec<String>,
    label: &str,
    previous: &Option<String>,
    current: &Option<String>,
) {
    let previous = previous.as_deref().unwrap_or_default().trim();
    let current = current.as_deref().unwrap_or_default().trim();
    if previous != current && (!previous.is_empty() || !current.is_empty()) {
        parts.push(format!("{label}:{previous}→{current}"));
    }
}

fn materially_different_text(previous: Option<&str>, current: Option<&str>) -> bool {
    let normalize = |value: Option<&str>| {
        value
            .unwrap_or_default()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .to_lowercase()
    };
    let previous = normalize(previous);
    let current = normalize(current);
    if previous == current {
        return false;
    }
    if previous.is_empty() || current.is_empty() {
        return previous.chars().count().max(current.chars().count()) >= 8;
    }

    let previous_chars = previous.chars().collect::<Vec<_>>();
    let current_chars = current.chars().collect::<Vec<_>>();
    let common_prefix = previous_chars
        .iter()
        .zip(current_chars.iter())
        .take_while(|(left, right)| left == right)
        .count();
    let previous_tail = &previous_chars[common_prefix..];
    let current_tail = &current_chars[common_prefix..];
    let common_suffix = previous_tail
        .iter()
        .rev()
        .zip(current_tail.iter().rev())
        .take_while(|(left, right)| left == right)
        .count();
    let changed_chars = previous_tail
        .len()
        .saturating_sub(common_suffix)
        .max(current_tail.len().saturating_sub(common_suffix));
    let longest = previous_chars.len().max(current_chars.len());
    changed_chars >= 12 || changed_chars.saturating_mul(10) >= longest
}

fn combine_distinct_capture_text(ax_text: Option<&str>, ocr_text: Option<&str>) -> Option<String> {
    let ax_text = ax_text.map(str::trim).filter(|value| !value.is_empty());
    let ocr_text = ocr_text.map(str::trim).filter(|value| !value.is_empty());
    match (ax_text, ocr_text) {
        (Some(ax), Some(ocr)) if ax == ocr => Some(ax.to_string()),
        (Some(ax), Some(ocr)) => Some(format!("{ax}\n{ocr}")),
        (Some(ax), None) => Some(ax.to_string()),
        (None, Some(ocr)) => Some(ocr.to_string()),
        (None, None) => None,
    }
}

fn truncate_optional_text(value: Option<String>, max_chars: usize) -> Option<String> {
    value
        .map(|value| value.trim().chars().take(max_chars).collect::<String>())
        .filter(|value| !value.is_empty())
}

/// 聚合一条 timeline 全部成员 capture 的可见文本。
///
/// 设计要点：
/// - 文档型成员（URL 含文档域名）优先靠前，保证有限预算下文档正文不被 IM/编码噪声挤掉；
/// - 同一份文档的多次 capture 按 head 去重，保留正文最长（同长时最新）的一帧；
/// - 返回 (聚合文本, 纳入的成员数)。即使去重后只剩一帧，也保留这帧，因为它可能
///   比 timeline 主 capture 更完整。
fn aggregate_member_capture_text(
    conn: &Connection,
    capture_ids: &[i64],
    primary_capture_id: i64,
) -> Result<Option<(String, i64)>, StorageError> {
    if capture_ids.len() <= 1 {
        return Ok(None);
    }

    // 读取成员的文本与 URL；按时间序，文档型优先。
    let placeholders = capture_ids
        .iter()
        .map(|_| "?")
        .collect::<Vec<_>>()
        .join(",");
    let sql = format!(
        "SELECT id, ts, ax_text, ocr_text, input_text, url
         FROM captures
         WHERE id IN ({placeholders})
         ORDER BY ts ASC
         LIMIT {MEMBER_AGGREGATION_MAX_CAPTURES}"
    );
    let mut stmt = conn.prepare(&sql)?;
    let params_vec: Vec<&dyn rusqlite::ToSql> = capture_ids
        .iter()
        .map(|id| id as &dyn rusqlite::ToSql)
        .collect();
    let rows = stmt.query_map(params_vec.as_slice(), |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, Option<String>>(5)?,
        ))
    })?;

    struct Member {
        cap_id: i64,
        ts: i64,
        text: String,
        is_doc: bool,
    }
    let mut members: Vec<Member> = Vec::new();
    for row in rows {
        let (cap_id, ts, ax_text, ocr_text, input_text, url) = row.map_err(StorageError::Sqlite)?;
        let combined = combine_capture_text_for_url(
            ax_text.as_deref(),
            ocr_text.as_deref(),
            input_text.as_deref(),
        );
        if combined.is_empty() {
            continue;
        }
        let is_doc = url.as_deref().map(is_document_url).unwrap_or(false);
        members.push(Member {
            cap_id,
            ts,
            text: combined,
            is_doc,
        });
    }

    if members.len() <= 1 {
        return Ok(None);
    }

    // 相同开头通常是同一页面的周期性快照。旧逻辑先到先得，会把更晚、更完整的长
    // 快照误删，只留下最早的短文本；改为每个 head 保留最长（同长时最新）的版本。
    let mut best_by_head: std::collections::HashMap<String, Member> =
        std::collections::HashMap::new();
    for member in members {
        let head: String = member
            .text
            .chars()
            .take(MEMBER_AGGREGATION_DEDUP_HEAD_CHARS)
            .collect();
        let should_replace = best_by_head.get(&head).map_or(true, |existing| {
            let member_len = member.text.chars().count();
            let existing_len = existing.text.chars().count();
            member_len > existing_len || (member_len == existing_len && member.ts > existing.ts)
        });
        if should_replace {
            best_by_head.insert(head, member);
        }
    }
    let mut members: Vec<Member> = best_by_head.into_values().collect();

    // 文档型优先（稳定排序：先 is_doc 降序，再时间升序），保证预算先喂文档正文。
    members.sort_by(|a, b| b.is_doc.cmp(&a.is_doc).then(a.ts.cmp(&b.ts)));

    let mut buf = String::new();
    let mut budget = MEMBER_AGGREGATION_TOTAL_BUDGET_CHARS;
    let mut included = 0_i64;
    for m in &members {
        let allowed = budget.min(MEMBER_AGGREGATION_PER_CAPTURE_CAP_CHARS);
        if allowed == 0 {
            break;
        }
        let truncated: String = m.text.chars().take(allowed).collect();
        let used = truncated.chars().count();
        let tag = if m.is_doc { "doc" } else { "ctx" };
        let primary_mark = if m.cap_id == primary_capture_id {
            " primary"
        } else {
            ""
        };
        buf.push_str(&format!(
            "--- capture#{} ts={} [{}{}] ---\n",
            m.cap_id, m.ts, tag, primary_mark
        ));
        buf.push_str(&truncated);
        buf.push_str("\n\n");
        budget = budget.saturating_sub(used);
        included += 1;
        if budget == 0 {
            break;
        }
    }

    if included == 0 {
        return Ok(None);
    }
    Ok(Some((buf, included)))
}

fn list_timeline_capture_ids(
    conn: &Connection,
    timeline_id: i64,
    primary_capture_id: i64,
) -> Result<Vec<i64>, StorageError> {
    let mut stmt = conn.prepare(
        "SELECT id FROM captures
         WHERE timeline_id = ?1 OR id = ?2
         ORDER BY ts ASC, id ASC",
    )?;
    let rows = stmt.query_map(params![timeline_id, primary_capture_id], |row| {
        row.get::<_, i64>(0)
    })?;
    let mut ids = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(StorageError::Sqlite)?;
    ids.dedup();
    Ok(ids)
}

/// 时间线的主 capture 常是文档加载页，只带“知识库/未命名文档”等占位标题。
/// 对全部成员帧按“出现帧数 > 最近时间 > webpage_title 优先”选择稳定标题，
/// 并按标题身份去重，避免同一帧的 webpage_title/win_title 被重复计数。
fn preferred_member_source_title(
    conn: &Connection,
    timeline_id: i64,
    primary_capture_id: i64,
) -> Result<Option<String>, StorageError> {
    struct TitleCandidate {
        display: String,
        capture_count: i64,
        latest_ts: i64,
        has_webpage_title: bool,
    }

    let mut stmt = conn.prepare(
        "SELECT ts, app_name, webpage_title, win_title
         FROM captures
         WHERE timeline_id = ?1 OR id = ?2
         ORDER BY ts ASC, id ASC",
    )?;
    let rows = stmt.query_map(params![timeline_id, primary_capture_id], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
        ))
    })?;

    let mut candidates: std::collections::HashMap<String, TitleCandidate> =
        std::collections::HashMap::new();
    for row in rows {
        let (ts, app_name, webpage_title, win_title) = row.map_err(StorageError::Sqlite)?;
        let mut identities_in_capture = std::collections::HashSet::new();
        for (raw_title, is_webpage_title) in [
            (webpage_title.as_deref(), true),
            (win_title.as_deref(), false),
        ] {
            let Some(display) = raw_title
                .and_then(|title| canonical_document_source_title(title, app_name.as_deref()))
            else {
                continue;
            };
            let Some(identity) = canonical_document_title_identity(&display) else {
                continue;
            };
            if !identities_in_capture.insert(identity.clone()) {
                continue;
            }
            let entry = candidates
                .entry(identity)
                .or_insert_with(|| TitleCandidate {
                    display: display.clone(),
                    capture_count: 0,
                    latest_ts: ts,
                    has_webpage_title: is_webpage_title,
                });
            entry.capture_count += 1;
            if ts > entry.latest_ts
                || (ts == entry.latest_ts && is_webpage_title && !entry.has_webpage_title)
            {
                entry.display = display;
                entry.latest_ts = ts;
                entry.has_webpage_title = is_webpage_title;
            }
        }
    }

    Ok(candidates
        .into_values()
        .max_by(|left, right| {
            left.capture_count
                .cmp(&right.capture_count)
                .then(left.latest_ts.cmp(&right.latest_ts))
                .then(left.has_webpage_title.cmp(&right.has_webpage_title))
                .then(
                    left.display
                        .chars()
                        .count()
                        .cmp(&right.display.chars().count()),
                )
        })
        .map(|candidate| candidate.display))
}

fn to_json_array_string(ids: &[i64]) -> String {
    serde_json::to_string(ids).unwrap_or_else(|_| "[]".to_string())
}

/// 判断 URL 是否指向文档（用于成员聚合时优先文档型 capture）。
fn is_document_url(url: &str) -> bool {
    let u = url.trim().to_lowercase();
    if u.is_empty() {
        return false;
    }
    // 常见企业/通用文档域名与路径特征。
    const DOC_MARKERS: &[&str] = &[
        "/docs/",
        "docs.google",
        "/document/",
        "yuque.com",
        "feishu.cn/docx",
        "feishu.cn/wiki",
        "notion.so",
        "confluence",
        "/wiki/",
        "shimo.im",
        "/d/home/",
        "/s/home/",
        "/k/home/",
    ];
    DOC_MARKERS.iter().any(|marker| u.contains(marker))
}

/// 解析 timeline.capture_ids（JSON 数组，元素可能是数字或字符串）为 i64 列表。
fn parse_capture_ids(raw: Option<&str>) -> Vec<i64> {
    let Some(raw) = raw.map(str::trim).filter(|s| !s.is_empty()) else {
        return Vec::new();
    };
    let Ok(value) = serde_json::from_str::<serde_json::Value>(raw) else {
        return Vec::new();
    };
    match value {
        serde_json::Value::Array(items) => items
            .into_iter()
            .filter_map(|item| match item {
                serde_json::Value::Number(n) => n.as_i64(),
                serde_json::Value::String(s) => s.trim().parse::<i64>().ok(),
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    }
}

fn aggregate_url_capture_text(
    conn: &Connection,
    url: &str,
    anchor_ts: i64,
) -> Result<Option<(String, i64)>, StorageError> {
    let earliest = anchor_ts.saturating_sub(URL_AGGREGATION_LOOKBACK_MS);
    let mut stmt = conn.prepare(
        "SELECT id, ts, ax_text, ocr_text, input_text
         FROM captures
         WHERE TRIM(COALESCE(url, '')) = ?1
           AND ts >= ?2
           AND ts <= ?3
         ORDER BY ts ASC
         LIMIT ?4",
    )?;
    let rows = stmt.query_map(
        params![url, earliest, anchor_ts, URL_AGGREGATION_MAX_CAPTURES],
        |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, Option<String>>(2)?,
                row.get::<_, Option<String>>(3)?,
                row.get::<_, Option<String>>(4)?,
            ))
        },
    )?;

    struct UrlCapture {
        cap_id: i64,
        ts: i64,
        text: String,
    }
    let mut best_by_head: std::collections::HashMap<String, UrlCapture> =
        std::collections::HashMap::new();
    for row in rows {
        let (cap_id, ts, ax_text, ocr_text, input_text) = row.map_err(StorageError::Sqlite)?;
        let text = combine_capture_text_for_url(
            ax_text.as_deref(),
            ocr_text.as_deref(),
            input_text.as_deref(),
        );
        if text.is_empty() {
            continue;
        }
        let head: String = text
            .chars()
            .take(URL_AGGREGATION_DEDUP_HEAD_CHARS)
            .collect();
        let should_replace = best_by_head.get(&head).map_or(true, |existing| {
            let text_len = text.chars().count();
            let existing_len = existing.text.chars().count();
            text_len > existing_len || (text_len == existing_len && ts > existing.ts)
        });
        if should_replace {
            best_by_head.insert(head, UrlCapture { cap_id, ts, text });
        }
    }
    let mut captures: Vec<UrlCapture> = best_by_head.into_values().collect();
    captures.sort_by_key(|capture| capture.ts);

    let mut buf = String::new();
    let mut budget = URL_AGGREGATION_TOTAL_BUDGET_CHARS;
    let mut included = 0_i64;
    for capture in captures {
        let allowed = budget.min(URL_AGGREGATION_PER_CAPTURE_CAP_CHARS);
        if allowed == 0 {
            break;
        }
        let truncated: String = capture.text.chars().take(allowed).collect();
        let used = truncated.chars().count();
        buf.push_str(&format!(
            "--- capture#{} ts={} ---\n",
            capture.cap_id, capture.ts
        ));
        buf.push_str(&truncated);
        buf.push_str("\n\n");
        budget = budget.saturating_sub(used);
        included += 1;
        if budget == 0 {
            break;
        }
    }
    if included == 0 {
        return Ok(None);
    }
    Ok(Some((buf, included)))
}

fn combine_capture_text_for_url(
    ax_text: Option<&str>,
    ocr_text: Option<&str>,
    input_text: Option<&str>,
) -> String {
    let pieces = [ax_text, ocr_text, input_text]
        .iter()
        .filter_map(|p| p.map(str::trim).filter(|t| !t.is_empty()))
        .collect::<Vec<_>>();
    pieces.join("\n")
}

fn insert_timeline_entry_inner(
    conn: &Connection,
    entry: &NewTimeline,
) -> Result<i64, StorageError> {
    let now = current_ts_ms();
    conn.execute(
        "INSERT INTO knowledge_entries (
            capture_id, summary, overview, details, entities, category, importance,
            occurrence_count, observed_at, event_time_start, event_time_end,
            history_view, content_origin, activity_type, is_self_generated,
            evidence_strength, user_verified, user_edited,
            created_at, updated_at, created_at_ms, updated_at_ms
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, 0, 0,
                   datetime(?17 / 1000, 'unixepoch'), datetime(?17 / 1000, 'unixepoch'), ?17, ?17)",
        params![
            entry.capture_id,
            entry.summary,
            entry.overview,
            entry.details,
            entry.entities,
            entry.category,
            entry.importance,
            entry.occurrence_count,
            entry.observed_at,
            entry.event_time_start,
            entry.event_time_end,
            entry.history_view,
            entry.content_origin,
            entry.activity_type,
            entry.is_self_generated,
            entry.evidence_strength,
            now,
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

fn row_to_timeline_entry(row: &rusqlite::Row<'_>) -> Result<TimelineRecord, StorageError> {
    Ok(TimelineRecord {
        id: row.get(0)?,
        capture_id: row.get(1)?,
        summary: row.get(2)?,
        overview: row.get(3)?,
        details: row.get(4)?,
        detailed_content: None,
        entities: row.get(5)?,
        category: row.get(6)?,
        importance: row.get::<_, Option<i64>>(7)?.unwrap_or(3),
        occurrence_count: row.get(8)?,
        observed_at: row.get(9)?,
        event_time_start: row.get(10)?,
        event_time_end: row.get(11)?,
        history_view: row.get::<_, Option<bool>>(12)?.unwrap_or(false),
        content_origin: row.get(13)?,
        activity_type: row.get(14)?,
        is_self_generated: row.get::<_, Option<bool>>(15)?.unwrap_or(false),
        evidence_strength: row.get(16)?,
        user_verified: row.get::<_, Option<bool>>(17)?.unwrap_or(false),
        user_edited: row.get::<_, Option<bool>>(18)?.unwrap_or(false),
        created_at: row.get(19)?,
        updated_at: row.get(20)?,
        created_at_ms: row.get::<_, Option<i64>>(21)?.unwrap_or(0),
        updated_at_ms: row.get::<_, Option<i64>>(22)?.unwrap_or(0),
        capture_ids: row.get(23)?,
        start_time: row.get(24)?,
        end_time: row.get(25)?,
        duration_minutes: row.get(26)?,
        frag_app_name: row.get(27)?,
        frag_win_title: row.get(28)?,
        time_range_start: row.get(29)?,
        time_range_end: row.get(30)?,
        key_timestamps: row.get(31)?,
    })
}

/// 将 episodic_memory 行转换为 TimelineRecord（用于向后兼容）
fn row_to_timeline_record(row: &rusqlite::Row<'_>) -> Result<TimelineRecord, StorageError> {
    Ok(TimelineRecord {
        id: row.get(0)?,
        capture_id: row.get(1)?,
        summary: row.get(2)?,
        overview: row.get(3)?,
        details: row.get(4)?,
        detailed_content: None,
        entities: row.get(5)?,
        category: row.get(6)?,
        importance: row.get::<_, Option<i64>>(7)?.unwrap_or(3),
        occurrence_count: row.get(8)?,
        observed_at: row.get(9)?,
        event_time_start: row.get(10)?,
        event_time_end: row.get(11)?,
        history_view: row.get::<_, Option<bool>>(12)?.unwrap_or(false),
        content_origin: row.get(13)?,
        activity_type: row.get(14)?,
        is_self_generated: row.get::<_, Option<bool>>(15)?.unwrap_or(false),
        evidence_strength: row.get(16)?,
        user_verified: row.get::<_, Option<bool>>(17)?.unwrap_or(false),
        user_edited: row.get::<_, Option<bool>>(18)?.unwrap_or(false),
        created_at: row.get(19)?,
        updated_at: row.get(20)?,
        created_at_ms: row.get::<_, Option<i64>>(21)?.unwrap_or(0),
        updated_at_ms: row.get::<_, Option<i64>>(22)?.unwrap_or(0),
        capture_ids: row.get(23)?,
        start_time: row.get(24)?,
        end_time: row.get(25)?,
        duration_minutes: row.get(26)?,
        frag_app_name: row.get(27)?,
        frag_win_title: row.get(28)?,
        time_range_start: row.get(29)?,
        time_range_end: row.get(30)?,
        key_timestamps: row.get(31)?,
    })
}

// ============================================================================
// 新表操作函数 - Episodic Memories
// ============================================================================

impl StorageManager {
    /// 插入情节记忆
    pub fn insert_episodic_memory(&self, entry: &NewEpisodicMemory) -> Result<i64, StorageError> {
        self.with_conn(|conn| insert_episodic_memory_inner(conn, entry))
    }

    /// 查询情节记忆（分页）
    pub fn list_timelines_paginated(
        &self,
        category: Option<&str>,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<EpisodicMemoryRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut sql = String::from(
                "SELECT id, capture_id, summary, overview, details, entities, category, importance,
                        occurrence_count, observed_at, event_time_start, event_time_end,
                        history_view, content_origin, activity_type, is_self_generated,
                        evidence_strength, user_verified, user_edited, created_at, updated_at,
                        created_at_ms, updated_at_ms, capture_ids, start_time, end_time,
                        duration_minutes, frag_app_name, frag_win_title, time_range_start,
                        time_range_end, key_timestamps
                 FROM timelines",
            );
            let mut params: Vec<Box<dyn rusqlite::ToSql>> = vec![];

            if let Some(cat) = category {
                sql.push_str(" WHERE category = ?");
                params.push(Box::new(cat.to_string()));
            }

            sql.push_str(" ORDER BY updated_at_ms DESC LIMIT ? OFFSET ?");
            params.push(Box::new(limit as i64));
            params.push(Box::new(offset as i64));

            let mut stmt = conn.prepare(&sql)?;
            let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|b| b.as_ref()).collect();
            let rows = stmt.query_map(param_refs.as_slice(), |row| {
                Ok(row_to_episodic_memory(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    /// 统计情节记忆数量
    pub fn count_timelines(&self, category: Option<&str>) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let (sql, params): (String, Vec<Box<dyn rusqlite::ToSql>>) = if let Some(cat) = category
            {
                (
                    "SELECT COUNT(*) FROM timelines WHERE category = ? AND COALESCE(is_self_generated, 0) = 0"
                        .to_string(),
                    vec![Box::new(cat.to_string())],
                )
            } else {
                (
                    "SELECT COUNT(*) FROM timelines WHERE COALESCE(is_self_generated, 0) = 0"
                        .to_string(),
                    vec![],
                )
            };

            let param_refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|b| b.as_ref()).collect();
            conn.query_row(&sql, param_refs.as_slice(), |row| row.get(0))
                .map_err(StorageError::Sqlite)
        })
    }

    /// 获取单条情节记忆
    pub fn get_episodic_memory(
        &self,
        id: i64,
    ) -> Result<Option<EpisodicMemoryRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, capture_id, summary, overview, details, entities, category, importance,
                        occurrence_count, observed_at, event_time_start, event_time_end,
                        history_view, content_origin, activity_type, is_self_generated,
                        evidence_strength, user_verified, user_edited, created_at, updated_at,
                        created_at_ms, updated_at_ms, capture_ids, start_time, end_time,
                        duration_minutes, frag_app_name, frag_win_title, time_range_start,
                        time_range_end, key_timestamps
                 FROM timelines WHERE id = ?1",
            )?;
            match stmt.query_row(params![id], |row| {
                row_to_episodic_memory(row).map_err(|_| rusqlite::Error::InvalidQuery)
            }) {
                Ok(entry) => Ok(Some(entry)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(e) => Err(StorageError::Sqlite(e)),
            }
        })
    }

    /// 更新情节记忆
    pub fn update_episodic_memory(
        &self,
        id: i64,
        summary: &str,
        overview: Option<&str>,
        details: Option<&str>,
        entities: &str,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE timelines
                 SET summary = ?1, overview = ?2, details = ?3, entities = ?4, user_edited = 1,
                     updated_at = datetime(?6 / 1000, 'unixepoch'), updated_at_ms = ?6
                 WHERE id = ?5",
                params![summary, overview, details, entities, id, now],
            )?;
            Ok(affected > 0)
        })
    }

    /// 设置情节记忆验证状态
    pub fn set_episodic_memory_verified(
        &self,
        id: i64,
        verified: bool,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE timelines SET user_verified = ?1,
                 updated_at = datetime(?3 / 1000, 'unixepoch'), updated_at_ms = ?3
                 WHERE id = ?2",
                params![verified, id, now],
            )?;
            Ok(affected > 0)
        })
    }

    /// 删除情节记忆
    pub fn delete_episodic_memory(&self, id: i64) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute("DELETE FROM timelines WHERE id = ?1", params![id])?;
            Ok(affected > 0)
        })
    }

    /// 获取时间线关联的Capture IDs
    pub fn get_timeline_capture_ids(&self, timeline_id: i64) -> Result<Vec<i64>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt =
                conn.prepare("SELECT capture_id, capture_ids FROM timelines WHERE id = ?1")?;
            match stmt.query_row(params![timeline_id], |row| {
                Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?))
            }) {
                Ok((primary_capture_id, Some(json))) => {
                    let mut ids = list_timeline_capture_ids(conn, timeline_id, primary_capture_id)?;
                    for id in serde_json::from_str::<Vec<i64>>(&json).unwrap_or_default() {
                        if !ids.contains(&id) {
                            ids.push(id);
                        }
                    }
                    Ok(ids)
                }
                Ok((primary_capture_id, None)) => {
                    list_timeline_capture_ids(conn, timeline_id, primary_capture_id)
                }
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(vec![]),
                Err(e) => Err(StorageError::Sqlite(e)),
            }
        })
    }

    /// 更新时间线的关联Capture IDs
    pub fn update_timeline_capture_ids(
        &self,
        timeline_id: i64,
        capture_ids: &[i64],
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let json = serde_json::to_string(capture_ids).unwrap_or_else(|_| "[]".to_string());
            let affected = conn.execute(
                "UPDATE timelines SET capture_ids = ?1 WHERE id = ?2",
                params![json, timeline_id],
            )?;
            Ok(affected > 0)
        })
    }
}

fn insert_episodic_memory_inner(
    conn: &Connection,
    entry: &NewEpisodicMemory,
) -> Result<i64, StorageError> {
    let now = current_ts_ms();
    conn.execute(
        "INSERT INTO timelines (
            capture_id, summary, overview, details, entities, category, importance,
            occurrence_count, observed_at, event_time_start, event_time_end,
            history_view, content_origin, activity_type, is_self_generated,
            evidence_strength, user_verified, user_edited,
            created_at, updated_at, created_at_ms, updated_at_ms,
            capture_ids, start_time, end_time, duration_minutes, frag_app_name,
            frag_win_title, time_range_start, time_range_end, key_timestamps,
            work_item, work_status, work_progress
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, 0, 0,
                   datetime(?17 / 1000, 'unixepoch'), datetime(?17 / 1000, 'unixepoch'), ?17, ?17,
                   ?18, ?19, ?20, ?21, ?22, ?23, ?24, ?25, ?26, ?27, ?28, ?29)",
        params![
            entry.capture_id,
            entry.summary,
            entry.overview,
            entry.details,
            entry.entities,
            entry.category,
            entry.importance,
            entry.occurrence_count,
            entry.observed_at,
            entry.event_time_start,
            entry.event_time_end,
            entry.history_view,
            entry.content_origin,
            entry.activity_type,
            entry.is_self_generated,
            entry.evidence_strength,
            now, // ?17: 用于 created_at, updated_at, created_at_ms, updated_at_ms
            entry.capture_ids,
            entry.start_time,
            entry.end_time,
            entry.duration_minutes,
            entry.frag_app_name,
            entry.frag_win_title,
            entry.time_range_start,
            entry.time_range_end,
            entry.key_timestamps,
            entry.work_item,
            entry.work_status,
            entry.work_progress,
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

fn row_to_episodic_memory(row: &rusqlite::Row<'_>) -> Result<EpisodicMemoryRecord, StorageError> {
    Ok(EpisodicMemoryRecord {
        id: row.get(0)?,
        capture_id: row.get(1)?,
        summary: row.get(2)?,
        overview: row.get(3)?,
        details: row.get(4)?,
        detailed_content: None,
        entities: row.get(5)?,
        category: row.get(6)?,
        importance: row.get::<_, Option<i64>>(7)?.unwrap_or(3),
        occurrence_count: row.get(8)?,
        observed_at: row.get(9)?,
        event_time_start: row.get(10)?,
        event_time_end: row.get(11)?,
        history_view: row.get::<_, Option<bool>>(12)?.unwrap_or(false),
        content_origin: row.get(13)?,
        activity_type: row.get(14)?,
        is_self_generated: row.get::<_, Option<bool>>(15)?.unwrap_or(false),
        evidence_strength: row.get(16)?,
        user_verified: row.get::<_, Option<bool>>(17)?.unwrap_or(false),
        user_edited: row.get::<_, Option<bool>>(18)?.unwrap_or(false),
        created_at: row.get(19)?,
        updated_at: row.get(20)?,
        created_at_ms: row.get::<_, Option<i64>>(21)?.unwrap_or(0),
        updated_at_ms: row.get::<_, Option<i64>>(22)?.unwrap_or(0),
        capture_ids: row.get(23)?,
        start_time: row.get(24)?,
        end_time: row.get(25)?,
        duration_minutes: row.get(26)?,
        frag_app_name: row.get(27)?,
        frag_win_title: row.get(28)?,
        time_range_start: row.get(29)?,
        time_range_end: row.get(30)?,
        key_timestamps: row.get(31)?,
    })
}

// ============================================================================
// Bake Knowledge 操作
// ============================================================================

impl StorageManager {
    pub fn insert_bake_knowledge(&self, knowledge: &NewBakeKnowledge) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            conn.execute(
                // episodic_memory_id 是列重命名为 timeline_id 前的旧列，仍带 NOT NULL 约束。
                // 二者语义等价（见 db.rs 中 timeline_id = episodic_memory_id 的回填），
                // 这里同时写入旧列，避免 NOT NULL 约束导致 knowledge 提炼结果无法落库。
                // source_capture_ids 为 NOT NULL DEFAULT '[]'，但 build_bake_knowledge_entry
                // 可能传入 None；显式绑定 NULL 会覆盖 DEFAULT 触发约束失败，用 COALESCE 兜底。
                // （废弃列 episodic_memory_id 已由迁移 033 移除，无需再写入。）
                "INSERT INTO bake_knowledge (
                    timeline_id, title, summary, content, detailed_content, entities, importance,
                    user_verified, user_edited,
                    created_at, updated_at, created_at_ms, updated_at_ms, source_capture_ids
                 ) VALUES (NULLIF(?1, 0), ?2, ?3, ?4, ?5, ?6, ?7, 1, 0,
                           datetime(?8 / 1000, 'unixepoch'), datetime(?8 / 1000, 'unixepoch'), ?8, ?8, COALESCE(?9, '[]'))",
                params![
                    knowledge.timeline_id,
                    knowledge.title,
                    knowledge.summary,
                    knowledge.content,
                    knowledge.detailed_content,
                    knowledge.entities,
                    knowledge.importance,
                    now,
                    knowledge.source_capture_ids,
                ],
            )?;
            Ok(conn.last_insert_rowid())
        })
    }

    pub fn count_bake_knowledge(&self) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            conn.query_row("SELECT COUNT(*) FROM bake_knowledge", [], |row| row.get(0))
                .map_err(StorageError::Sqlite)
        })
    }

    pub fn get_bake_knowledge(&self, id: i64) -> Result<Option<BakeKnowledgeRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT b.id, COALESCE(b.timeline_id, 0), b.title, b.summary, b.content, b.detailed_content, b.entities, b.importance,
                        b.user_verified, b.user_edited, b.created_at, b.updated_at, b.created_at_ms, b.updated_at_ms,
                        b.source_capture_ids,
                        COALESCE((
                            SELECT SUM(COALESCE(t.occurrence_count, 1))
                            FROM bake_artifact_source_links l
                            JOIN timelines t ON t.id = l.source_timeline_id
                            WHERE l.artifact_kind = 'knowledge' AND l.artifact_id = b.id
                        ), (
                            SELECT COALESCE(t.occurrence_count, 1)
                            FROM timelines t WHERE t.id = b.timeline_id
                        ), 1) AS occurrence_count
                 FROM bake_knowledge b WHERE b.id = ?1"
            )?;
            match stmt.query_row(params![id], |row| {
                row_to_bake_knowledge(row).map_err(|_| rusqlite::Error::InvalidQuery)
            }) {
                Ok(knowledge) => Ok(Some(knowledge)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(e) => Err(StorageError::Sqlite(e)),
            }
        })
    }

    pub fn find_bake_knowledge_by_timeline_id(
        &self,
        timeline_id: i64,
    ) -> Result<Option<BakeKnowledgeRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT b.id, COALESCE(b.timeline_id, 0), b.title, b.summary, b.content, b.detailed_content, b.entities, b.importance,
                        b.user_verified, b.user_edited, b.created_at, b.updated_at, b.created_at_ms, b.updated_at_ms,
                        b.source_capture_ids,
                        COALESCE((
                            SELECT SUM(COALESCE(t.occurrence_count, 1))
                            FROM bake_artifact_source_links l
                            JOIN timelines t ON t.id = l.source_timeline_id
                            WHERE l.artifact_kind = 'knowledge' AND l.artifact_id = b.id
                        ), (
                            SELECT COALESCE(t.occurrence_count, 1)
                            FROM timelines t WHERE t.id = b.timeline_id
                        ), 1) AS occurrence_count
                 FROM bake_knowledge b WHERE b.timeline_id = ?1 ORDER BY b.updated_at_ms DESC, b.id DESC LIMIT 1"
            )?;
            match stmt.query_row(params![timeline_id], |row| {
                row_to_bake_knowledge(row).map_err(|_| rusqlite::Error::InvalidQuery)
            }) {
                Ok(knowledge) => Ok(Some(knowledge)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(e) => Err(StorageError::Sqlite(e)),
            }
        })
    }

    pub fn update_bake_knowledge(
        &self,
        id: i64,
        title: &str,
        summary: &str,
        content: Option<&str>,
        entities: &str,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE bake_knowledge
                 SET title = ?1, summary = ?2, content = ?3, entities = ?4, user_edited = 1,
                     updated_at = datetime(?6 / 1000, 'unixepoch'), updated_at_ms = ?6
                 WHERE id = ?5",
                params![title, summary, content, entities, id, now],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn update_bake_knowledge_system(
        &self,
        id: i64,
        title: &str,
        summary: &str,
        content: Option<&str>,
        detailed_content: Option<&str>,
        entities: &str,
        importance: i64,
        source_capture_ids: Option<&str>,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE bake_knowledge
                 SET title = ?1, summary = ?2, content = ?3, detailed_content = ?4,
                     entities = ?5, importance = ?6,
                     source_capture_ids = COALESCE(?7, source_capture_ids, '[]'),
                     updated_at = datetime(?9 / 1000, 'unixepoch'), updated_at_ms = ?9
                 WHERE id = ?8",
                params![
                    title,
                    summary,
                    content,
                    detailed_content,
                    entities,
                    importance,
                    source_capture_ids,
                    id,
                    now,
                ],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn update_bake_knowledge_manual(
        &self,
        id: i64,
        title: &str,
        summary: &str,
        content: Option<&str>,
        detailed_content: Option<&str>,
        entities: &str,
        importance: i64,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE bake_knowledge
                 SET title = ?1, summary = ?2, content = ?3, detailed_content = ?4,
                     entities = ?5, importance = ?6, user_verified = 1, user_edited = 1,
                     updated_at = datetime(?8 / 1000, 'unixepoch'), updated_at_ms = ?8
                 WHERE id = ?7",
                params![
                    title,
                    summary,
                    content,
                    detailed_content,
                    entities,
                    importance,
                    id,
                    now,
                ],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn delete_bake_knowledge(&self, id: i64) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            tx.execute(
                "DELETE FROM bake_artifact_source_fingerprints
                 WHERE artifact_kind = 'knowledge' AND artifact_id = ?1",
                params![id],
            )?;
            tx.execute(
                "DELETE FROM bake_artifact_source_links
                 WHERE artifact_kind = 'knowledge' AND artifact_id = ?1",
                params![id],
            )?;
            let affected = tx.execute("DELETE FROM bake_knowledge WHERE id = ?1", params![id])?;
            if affected > 0 {
                StorageManager::delete_memory_favorite_with_conn(&tx, "knowledge", id)?;
            }
            tx.commit()?;
            Ok(affected > 0)
        })
    }
}

fn row_to_bake_knowledge(row: &rusqlite::Row<'_>) -> Result<BakeKnowledgeRecord, StorageError> {
    Ok(BakeKnowledgeRecord {
        id: row.get(0)?,
        timeline_id: row.get(1)?,
        title: row.get(2)?,
        summary: row.get(3)?,
        content: row.get(4)?,
        detailed_content: row.get(5)?,
        entities: row.get(6)?,
        importance: row.get::<_, Option<i64>>(7)?.unwrap_or(3),
        user_verified: row.get::<_, Option<bool>>(8)?.unwrap_or(false),
        user_edited: row.get::<_, Option<bool>>(9)?.unwrap_or(false),
        created_at: row.get(10)?,
        updated_at: row.get(11)?,
        created_at_ms: row.get::<_, Option<i64>>(12)?.unwrap_or(0),
        updated_at_ms: row.get::<_, Option<i64>>(13)?.unwrap_or(0),
        source_capture_ids: row.get(14)?,
        occurrence_count: row.get::<_, Option<i64>>(15)?.unwrap_or(1),
    })
}

// ============================================================================
// Bake SOPs 操作
// ============================================================================

impl StorageManager {
    pub fn insert_bake_sop(&self, sop: &NewBakeSop) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            conn.execute(
                // 废弃列 episodic_memory_id 已由迁移 033 移除；source_capture_ids 用 COALESCE 兜底。
                "INSERT INTO bake_sops (
                    timeline_id, title, summary, content, detailed_content, entities, importance,
                    user_verified, user_edited,
                    created_at, updated_at, created_at_ms, updated_at_ms, source_capture_ids
                 ) VALUES (NULLIF(?1, 0), ?2, ?3, ?4, ?5, ?6, ?7, 1, 0,
                           datetime(?8 / 1000, 'unixepoch'), datetime(?8 / 1000, 'unixepoch'), ?8, ?8, COALESCE(?9, '[]'))",
                params![
                    sop.timeline_id,
                    sop.title,
                    sop.summary,
                    sop.content,
                    sop.detailed_content,
                    sop.entities,
                    sop.importance,
                    now,
                    sop.source_capture_ids,
                ],
            )?;
            Ok(conn.last_insert_rowid())
        })
    }

    pub fn list_bake_sops_paginated(
        &self,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<BakeSopRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, COALESCE(timeline_id, 0) AS timeline_id, title, summary, content, detailed_content, entities, importance,
                        user_verified, user_edited, created_at, updated_at, created_at_ms, updated_at_ms, source_capture_ids
                 FROM bake_sops ORDER BY updated_at_ms DESC LIMIT ? OFFSET ?"
            )?;
            let rows = stmt.query_map(params![limit as i64, offset as i64], |row| {
                Ok(row_to_bake_sop(row).map_err(|_| rusqlite::Error::InvalidQuery)?)
            })?;
            rows.collect::<Result<Vec<_>, _>>().map_err(StorageError::Sqlite)
        })
    }

    pub fn count_bake_sops(&self) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            conn.query_row("SELECT COUNT(*) FROM bake_sops", [], |row| row.get(0))
                .map_err(StorageError::Sqlite)
        })
    }

    pub fn find_bake_artifact_by_source_fingerprint(
        &self,
        artifact_kind: &str,
        fingerprint: &str,
    ) -> Result<Option<i64>, StorageError> {
        self.with_conn(|conn| {
            match conn.query_row(
                "SELECT artifact_id
                 FROM bake_artifact_source_fingerprints
                 WHERE artifact_kind = ?1 AND fingerprint = ?2",
                params![artifact_kind, fingerprint],
                |row| row.get(0),
            ) {
                Ok(id) => Ok(Some(id)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(error) => Err(StorageError::Sqlite(error)),
            }
        })
    }

    pub fn find_bake_artifact_by_source_timeline(
        &self,
        artifact_kind: &str,
        source_timeline_id: i64,
    ) -> Result<Option<i64>, StorageError> {
        self.with_conn(|conn| {
            match conn.query_row(
                "SELECT artifact_id
                 FROM bake_artifact_source_links
                 WHERE artifact_kind = ?1 AND source_timeline_id = ?2",
                params![artifact_kind, source_timeline_id],
                |row| row.get(0),
            ) {
                Ok(id) => Ok(Some(id)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(error) => Err(StorageError::Sqlite(error)),
            }
        })
    }

    pub fn record_bake_artifact_source(
        &self,
        artifact_kind: &str,
        artifact_id: i64,
        source_timeline_id: i64,
        fingerprint: Option<&str>,
    ) -> Result<(), StorageError> {
        let created_at = current_ts_ms();
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            tx.execute(
                "INSERT OR IGNORE INTO bake_artifact_source_links (
                    artifact_kind, artifact_id, source_timeline_id, created_at
                 ) VALUES (?1, ?2, ?3, ?4)",
                params![artifact_kind, artifact_id, source_timeline_id, created_at],
            )?;
            if let Some(fingerprint) = fingerprint {
                tx.execute(
                    "INSERT OR IGNORE INTO bake_artifact_source_fingerprints (
                        artifact_kind, fingerprint, artifact_id, first_timeline_id, created_at
                     ) VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![
                        artifact_kind,
                        fingerprint,
                        artifact_id,
                        source_timeline_id,
                        created_at
                    ],
                )?;
            }
            tx.commit()?;
            Ok(())
        })
    }

    /// 给定候选 timeline_id 集合，返回其中已在 bake_knowledge 中有记录的 timeline_id 子集。
    /// 代替全量拉取所有 knowledge 再构建 HashSet，避免随数据增长内存和时间开销线性膨胀。
    pub fn find_existing_knowledge_timeline_ids(
        &self,
        candidate_ids: &[i64],
    ) -> Result<std::collections::HashSet<i64>, StorageError> {
        if candidate_ids.is_empty() {
            return Ok(std::collections::HashSet::new());
        }
        self.with_conn(|conn| {
            let placeholders = candidate_ids
                .iter()
                .map(|_| "?")
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!(
                "SELECT timeline_id FROM bake_knowledge WHERE timeline_id IN ({0})
                 UNION
                 SELECT source_timeline_id
                 FROM bake_artifact_source_links
                 WHERE artifact_kind = 'knowledge'
                   AND source_timeline_id IN ({0})",
                placeholders,
            );
            let mut stmt = conn.prepare(&sql)?;
            let mut params: Vec<&dyn rusqlite::ToSql> = candidate_ids
                .iter()
                .map(|id| id as &dyn rusqlite::ToSql)
                .collect();
            params.extend(candidate_ids.iter().map(|id| id as &dyn rusqlite::ToSql));
            let rows = stmt.query_map(params.as_slice(), |row| row.get::<_, i64>(0))?;
            rows.collect::<Result<std::collections::HashSet<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    /// 给定候选 timeline_id 集合，返回其中已在 bake_sops 中有记录的 timeline_id 子集。
    pub fn find_existing_sop_timeline_ids(
        &self,
        candidate_ids: &[i64],
    ) -> Result<std::collections::HashSet<i64>, StorageError> {
        if candidate_ids.is_empty() {
            return Ok(std::collections::HashSet::new());
        }
        self.with_conn(|conn| {
            let placeholders = candidate_ids
                .iter()
                .map(|_| "?")
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!(
                "SELECT timeline_id FROM bake_sops WHERE timeline_id IN ({0})
                 UNION
                 SELECT source_timeline_id
                 FROM bake_artifact_source_links
                 WHERE artifact_kind = 'sop'
                   AND source_timeline_id IN ({0})",
                placeholders,
            );
            let mut stmt = conn.prepare(&sql)?;
            let mut params: Vec<&dyn rusqlite::ToSql> = candidate_ids
                .iter()
                .map(|id| id as &dyn rusqlite::ToSql)
                .collect();
            params.extend(candidate_ids.iter().map(|id| id as &dyn rusqlite::ToSql));
            let rows = stmt.query_map(params.as_slice(), |row| row.get::<_, i64>(0))?;
            rows.collect::<Result<std::collections::HashSet<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    pub fn get_bake_sop(&self, id: i64) -> Result<Option<BakeSopRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, COALESCE(timeline_id, 0) AS timeline_id, title, summary, content, detailed_content, entities, importance,
                        user_verified, user_edited, created_at, updated_at, created_at_ms, updated_at_ms, source_capture_ids
                 FROM bake_sops WHERE id = ?1"
            )?;
            match stmt.query_row(params![id], |row| {
                row_to_bake_sop(row).map_err(|_| rusqlite::Error::InvalidQuery)
            }) {
                Ok(sop) => Ok(Some(sop)),
                Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
                Err(e) => Err(StorageError::Sqlite(e)),
            }
        })
    }

    pub fn update_bake_sop(
        &self,
        id: i64,
        title: &str,
        summary: &str,
        content: Option<&str>,
        entities: &str,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE bake_sops
                 SET title = ?1, summary = ?2, content = ?3, entities = ?4, user_edited = 1,
                     updated_at = datetime(?6 / 1000, 'unixepoch'), updated_at_ms = ?6
                 WHERE id = ?5",
                params![title, summary, content, entities, id, now],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn update_bake_sop_manual(
        &self,
        id: i64,
        title: &str,
        summary: &str,
        content: Option<&str>,
        detailed_content: Option<&str>,
        entities: &str,
        importance: i64,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE bake_sops
                 SET title = ?1, summary = ?2, content = ?3, detailed_content = ?4,
                     entities = ?5, importance = ?6, user_verified = 1, user_edited = 1,
                     updated_at = datetime(?8 / 1000, 'unixepoch'), updated_at_ms = ?8
                 WHERE id = ?7",
                params![
                    title,
                    summary,
                    content,
                    detailed_content,
                    entities,
                    importance,
                    id,
                    now,
                ],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn update_bake_sop_source_capture_ids(
        &self,
        id: i64,
        source_capture_ids: &str,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE bake_sops
                 SET source_capture_ids = ?1,
                     updated_at = datetime(?3 / 1000, 'unixepoch'),
                     updated_at_ms = ?3
                 WHERE id = ?2",
                params![source_capture_ids, id, now],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn delete_bake_sop(&self, id: i64) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            tx.execute(
                "DELETE FROM bake_artifact_source_fingerprints
                 WHERE artifact_kind = 'sop' AND artifact_id = ?1",
                params![id],
            )?;
            tx.execute(
                "DELETE FROM bake_artifact_source_links
                 WHERE artifact_kind = 'sop' AND artifact_id = ?1",
                params![id],
            )?;
            let affected = tx.execute("DELETE FROM bake_sops WHERE id = ?1", params![id])?;
            if affected > 0 {
                StorageManager::delete_memory_favorite_with_conn(&tx, "operation", id)?;
            }
            tx.commit()?;
            Ok(affected > 0)
        })
    }
}

fn row_to_bake_sop(row: &rusqlite::Row<'_>) -> Result<BakeSopRecord, StorageError> {
    Ok(BakeSopRecord {
        id: row.get("id")?,
        timeline_id: row.get::<_, Option<i64>>("timeline_id")?.unwrap_or(0),
        title: row.get("title")?,
        summary: row.get("summary")?,
        content: row.get("content")?,
        detailed_content: row.get("detailed_content")?,
        entities: row
            .get::<_, Option<String>>("entities")?
            .unwrap_or_default(),
        importance: row.get::<_, Option<i64>>("importance")?.unwrap_or(3),
        user_verified: row
            .get::<_, Option<bool>>("user_verified")?
            .unwrap_or(false),
        user_edited: row.get::<_, Option<bool>>("user_edited")?.unwrap_or(false),
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
        created_at_ms: row.get::<_, Option<i64>>("created_at_ms")?.unwrap_or(0),
        updated_at_ms: row.get::<_, Option<i64>>("updated_at_ms")?.unwrap_or(0),
        source_capture_ids: row.get("source_capture_ids")?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    use crate::storage::models::{EventType, NewCapture};

    fn make_mgr() -> StorageManager {
        StorageManager::open_in_memory().expect("内存数据库初始化失败")
    }

    fn seed_capture(mgr: &StorageManager) -> i64 {
        mgr.insert_capture(&NewCapture {
            ts: 1_700_000_000_000,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("知识条目来源".to_string()),
            event_type: EventType::Manual,
            ax_text: Some("知识来源内容".to_string()),
            ax_focused_role: None,
            ax_focused_id: None,
            ocr_text: None,
            screenshot_path: None,
            screenshot_source: None,
            input_text: None,
            is_sensitive: false,
            pii_scrubbed: false,
            url: None,
            webpage_title: None,
        })
        .expect("插入 capture 失败")
    }

    fn seed_document_capture(mgr: &StorageManager, ts: i64, text: String, url: &str) -> i64 {
        mgr.insert_capture(&NewCapture {
            ts,
            app_name: Some("Chrome".to_string()),
            app_bundle_id: Some("com.google.Chrome".to_string()),
            win_title: Some("长文档 - 云文档".to_string()),
            event_type: EventType::Manual,
            ax_text: Some(text),
            ax_focused_role: None,
            ax_focused_id: None,
            ocr_text: None,
            screenshot_path: None,
            screenshot_source: None,
            input_text: None,
            is_sensitive: false,
            pii_scrubbed: false,
            url: Some(url.to_string()),
            webpage_title: Some("长文档".to_string()),
        })
        .expect("插入文档 capture 失败")
    }

    fn sample_entry(mgr: &StorageManager, category: &str) -> NewTimeline {
        NewTimeline {
            capture_id: seed_capture(mgr),
            summary: "客服问题处理".to_string(),
            overview: Some("标准处理流程".to_string()),
            details: Some(r#"{"steps":["确认问题类型"]}"#.to_string()),
            entities: r#"["客服","SOP"]"#.to_string(),
            category: category.to_string(),
            importance: 4,
            occurrence_count: Some(3),
            observed_at: Some(1_700_000_000_000),
            event_time_start: None,
            event_time_end: None,
            history_view: false,
            content_origin: Some("manual".to_string()),
            activity_type: Some("support".to_string()),
            is_self_generated: false,
            evidence_strength: Some("high".to_string()),
            capture_ids: None,
            start_time: None,
            end_time: None,
            duration_minutes: None,
            frag_app_name: None,
            frag_win_title: None,
            time_range_start: None,
            time_range_end: None,
            key_timestamps: None,
            work_item: None,
            work_status: None,
            work_progress: None,
        }
    }

    #[test]
    fn test_insert_and_list_timelines_by_category() {
        let mgr = make_mgr();
        mgr.insert_timeline_entry(&sample_entry(&mgr, "bake_sop"))
            .unwrap();
        let entries = mgr.list_timelines_by_category("bake_sop").unwrap();
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].summary, "客服问题处理");
    }

    #[test]
    fn timeline_work_context_reaches_bake_candidate() {
        let mgr = StorageManager::open_in_memory().unwrap();
        let mut entry = sample_entry(&mgr, "代码");
        entry.work_item = Some("MemoryBread-操作提炼".to_string());
        entry.work_status = Some("completed".to_string());
        entry.work_progress = Some("已完成门禁改造，测试通过".to_string());
        let timeline_id = mgr.insert_timeline_entry(&entry).unwrap();

        let candidates = mgr.list_bake_memory_fresh_candidates(0, 10, 3).unwrap();
        let candidate = candidates
            .iter()
            .find(|candidate| candidate.timeline.id == timeline_id)
            .expect("新 timeline 应进入 bake 候选");

        assert_eq!(candidate.work_item.as_deref(), Some("MemoryBread-操作提炼"));
        assert_eq!(candidate.work_status.as_deref(), Some("completed"));
        assert_eq!(
            candidate.work_progress.as_deref(),
            Some("已完成门禁改造，测试通过")
        );
    }

    #[test]
    fn test_set_knowledge_verified() {
        let mgr = make_mgr();
        let id = mgr
            .insert_timeline_entry(&sample_entry(&mgr, "bake_article"))
            .unwrap();
        assert!(mgr.set_knowledge_verified(id, true).unwrap());
        let entry = mgr.get_timeline_entry(id).unwrap().unwrap();
        assert!(entry.user_verified);
    }

    #[test]
    fn test_count_non_bake_knowledge_filtered_excludes_bake_knowledge() {
        let mgr = make_mgr();
        mgr.insert_timeline_entry(&sample_entry(&mgr, "bake_knowledge"))
            .unwrap();
        mgr.insert_timeline_entry(&sample_entry(&mgr, "meeting"))
            .unwrap();

        assert_eq!(mgr.count_non_bake_knowledge_filtered(None).unwrap(), 1);
        assert_eq!(
            mgr.count_non_bake_knowledge_filtered(Some("客服")).unwrap(),
            1
        );
    }

    #[test]
    fn test_list_bake_memory_init_candidates_excludes_bake_knowledge() {
        let mgr = make_mgr();
        mgr.insert_timeline_entry(&sample_entry(&mgr, "bake_knowledge"))
            .unwrap();
        mgr.insert_timeline_entry(&sample_entry(&mgr, "meeting"))
            .unwrap();

        let candidates = mgr.list_bake_memory_init_candidates(0, 10).unwrap();
        assert_eq!(candidates.len(), 1);
        assert_eq!(candidates[0].timeline.category, "meeting");
    }

    #[test]
    fn test_retry_lane_ignores_watermark_and_excludes_existing_artifact() {
        let mgr = make_mgr();
        let timeline_id = mgr
            .insert_timeline_entry(&sample_entry(&mgr, "meeting"))
            .unwrap();
        mgr.upsert_bake_watermark("unified", current_ts_ms() + 60_000)
            .unwrap();
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO bake_retry_state (
                    timeline_id, failure_count, last_error, last_failed_at_ms,
                    last_error_code, next_retry_at_ms
                 ) VALUES (?1, 1, 'output invalid', 1, 'BAKE_OUTPUT_INVALID', 0)",
                params![timeline_id],
            )?;
            Ok(())
        })
        .unwrap();

        let retry = mgr.list_bake_memory_retry_candidates(10, 3).unwrap();
        assert_eq!(retry.len(), 1);
        assert_eq!(retry[0].timeline.id, timeline_id);
        assert_eq!(retry[0].retry_failure_count, 1);
        assert_eq!(
            retry[0].retry_error_code.as_deref(),
            Some("BAKE_OUTPUT_INVALID")
        );

        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO bake_knowledge (timeline_id, title, summary)
                 VALUES (?1, '已完成', '已有知识产物')",
                params![timeline_id],
            )?;
            Ok(())
        })
        .unwrap();
        assert!(mgr
            .list_bake_memory_retry_candidates(10, 3)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn test_retry_lane_respects_persistent_next_retry_time() {
        let mgr = make_mgr();
        let timeline_id = mgr
            .insert_timeline_entry(&sample_entry(&mgr, "meeting"))
            .unwrap();
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO bake_retry_state (
                    timeline_id, failure_count, last_error, last_failed_at_ms,
                    last_error_code, next_retry_at_ms
                 ) VALUES (?1, 1, 'timeout', 1, 'INFERENCE_TIMEOUT', ?2)",
                params![timeline_id, current_ts_ms() + 60_000],
            )?;
            Ok(())
        })
        .unwrap();

        assert!(mgr
            .list_bake_memory_retry_candidates(10, 3)
            .unwrap()
            .is_empty());
        let queue = mgr.get_bake_queue_status(3).unwrap();
        assert_eq!(queue.retry_delayed_count, 1);
        assert_eq!(queue.retry_ready_count, 0);
        assert_eq!(queue.actionable_count, 0);
        assert!(queue.recommended_retry_after_ms > 0);
    }

    #[test]
    fn test_dead_letter_is_not_reported_as_waiting_queue() {
        let mgr = make_mgr();
        let timeline_id = mgr
            .insert_timeline_entry(&sample_entry(&mgr, "meeting"))
            .unwrap();
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO bake_retry_state (
                    timeline_id, failure_count, last_error, last_failed_at_ms,
                    last_error_code, next_retry_at_ms
                 ) VALUES (?1, 3, 'invalid output', 1, 'BAKE_OUTPUT_INVALID', 0)",
                params![timeline_id],
            )?;
            Ok(())
        })
        .unwrap();

        let queue = mgr.get_bake_queue_status(3).unwrap();
        assert_eq!(queue.dead_letter_count, 1);
        assert_eq!(queue.pending_count, 0);
        assert_eq!(queue.actionable_count, 0);
        assert_eq!(queue.oldest_retry_at_ms, None);
    }

    #[test]
    fn test_document_identity_ignores_fragment() {
        assert_eq!(
            canonical_document_identity(
                "https://docs.example.com/d/home/sample-document#section=a"
            ),
            canonical_document_identity(
                "https://docs.example.com/d/home/sample-document#section=b"
            )
        );
    }

    #[test]
    fn test_artifact_source_fingerprint_links_cross_timeline_source() {
        let mgr = make_mgr();
        mgr.record_bake_artifact_source("knowledge", 77, 1001, Some("source-v1:abc"))
            .unwrap();

        assert_eq!(
            mgr.find_bake_artifact_by_source_fingerprint("knowledge", "source-v1:abc")
                .unwrap(),
            Some(77)
        );
        assert_eq!(
            mgr.find_bake_artifact_by_source_timeline("knowledge", 1001)
                .unwrap(),
            Some(77)
        );
        assert_eq!(
            mgr.find_existing_knowledge_timeline_ids(&[1000, 1001])
                .unwrap(),
            std::collections::HashSet::from([1001])
        );
    }

    #[test]
    fn bake_candidate_action_trace_preserves_cross_app_time_order_and_state_changes() {
        let mgr = make_mgr();
        let first = seed_capture(&mgr);
        let second = seed_capture(&mgr);
        let third = seed_capture(&mgr);
        mgr.with_conn(|conn| {
            conn.execute(
                "UPDATE captures
                 SET ts = ?2, app_name = ?3, win_title = ?4, ax_text = ?5,
                     event_type = 'mouse_click', ax_focused_role = 'AXButton',
                     ax_focused_id = 'save-config'
                 WHERE id = ?1",
                params![
                    first,
                    1_700_000_000_000_i64,
                    "Cursor",
                    "config.rs",
                    "相同界面壳：修改前"
                ],
            )?;
            conn.execute(
                "UPDATE captures
                 SET ts = ?2, app_name = ?3, win_title = ?4, ax_text = ?5, input_text = ?6,
                     event_type = 'key_pause', ax_focused_role = 'AXTextField',
                     ax_focused_id = 'terminal-input'
                 WHERE id = ?1",
                params![
                    second,
                    1_700_000_010_000_i64,
                    "Terminal",
                    "cargo test",
                    "执行测试",
                    "cargo test bake"
                ],
            )?;
            conn.execute(
                "UPDATE captures
                 SET ts = ?2, app_name = ?3, win_title = ?4, ax_text = ?5,
                     event_type = 'auto'
                 WHERE id = ?1",
                params![
                    third,
                    1_700_000_020_000_i64,
                    "Cursor",
                    "config.rs",
                    "相同界面壳：修改后"
                ],
            )?;
            Ok(())
        })
        .unwrap();

        let mut timeline = sample_entry(&mgr, "coding");
        timeline.capture_id = first;
        let timeline_id = mgr.insert_timeline_entry(&timeline).unwrap();
        mgr.with_conn(|conn| {
            conn.execute(
                "UPDATE captures SET timeline_id = ?1 WHERE id IN (?2, ?3, ?4)",
                params![timeline_id, first, second, third],
            )?;
            Ok(())
        })
        .unwrap();

        let candidates = mgr.list_bake_memory_fresh_candidates(0, 20, 3).unwrap();
        let candidate = candidates
            .iter()
            .find(|candidate| candidate.timeline.id == timeline_id)
            .unwrap();
        assert_eq!(
            candidate
                .action_trace
                .iter()
                .map(|item| item.capture_id)
                .collect::<Vec<_>>(),
            vec![first, second, third]
        );
        assert_eq!(
            candidate.action_trace[0].app_name.as_deref(),
            Some("Cursor")
        );
        assert_eq!(
            candidate.action_trace[1].app_name.as_deref(),
            Some("Terminal")
        );
        assert_eq!(
            candidate.action_trace[2].app_name.as_deref(),
            Some("Cursor")
        );
        assert_eq!(
            candidate.action_trace[0].visible_text.as_deref(),
            Some("相同界面壳：修改前")
        );
        assert_eq!(
            candidate.action_trace[2].visible_text.as_deref(),
            Some("相同界面壳：修改后")
        );
        assert_eq!(
            candidate.action_trace[0].ax_focused_id.as_deref(),
            Some("save-config")
        );
        assert_eq!(
            candidate.action_trace[1].evidence_kind.as_deref(),
            Some("input")
        );
        assert!(candidate
            .action_trace
            .iter()
            .all(|item| item.operation_evidence));
        assert!(candidate.action_trace[2]
            .state_delta
            .as_deref()
            .is_some_and(|delta| delta.contains("修改后")));
    }

    #[test]
    fn action_trace_compacts_consecutive_scroll_context_without_counting_it_as_operation() {
        let records = (1..=3)
            .map(|index| BakeActionTraceRecord {
                capture_id: index,
                ts: 1_700_000_000_000 + index * 1_000,
                event_type: "scroll".to_string(),
                app_name: Some("Chrome".to_string()),
                win_title: Some("说明页".to_string()),
                url: Some("https://example.com/guide".to_string()),
                webpage_title: Some("说明页".to_string()),
                visible_text: Some(format!("滚动后的静态正文 {index}")),
                input_text: None,
                audio_text: None,
                ax_focused_role: None,
                ax_focused_id: None,
                state_delta: None,
                evidence_kind: None,
                operation_evidence: false,
            })
            .collect();

        let compacted = annotate_and_compact_action_trace(records);

        assert_eq!(compacted.len(), 1);
        assert_eq!(compacted[0].capture_id, 3);
        assert_eq!(compacted[0].evidence_kind.as_deref(), Some("context"));
        assert!(!compacted[0].operation_evidence);
    }

    #[test]
    fn test_list_bake_candidates_recovers_url_from_matching_timeline_capture() {
        let mgr = make_mgr();
        let primary_capture_id = seed_capture(&mgr);
        mgr.with_conn(|conn| {
            conn.execute(
                "UPDATE captures
                 SET app_name = 'ChatGPT Atlas',
                     win_title = '容器云 GPU 指标采集项目 - 云文档'
                 WHERE id = ?1",
                params![primary_capture_id],
            )?;
            Ok(())
        })
        .unwrap();

        let mut timeline = sample_entry(&mgr, "document");
        timeline.capture_id = primary_capture_id;
        timeline.frag_win_title = Some("容器云 GPU 指标采集项目 - 云文档".to_string());
        let timeline_id = mgr.insert_timeline_entry(&timeline).unwrap();

        let source_capture_id = mgr
            .insert_capture(&NewCapture {
                ts: 1_700_000_010_000,
                app_name: Some("Google Chrome".to_string()),
                app_bundle_id: Some("com.google.Chrome".to_string()),
                win_title: Some("容器云 GPU 指标采集项目 - 云文档 - Google Chrome".to_string()),
                event_type: EventType::Manual,
                ax_text: Some("GPU 指标定义".to_string()),
                ax_focused_role: None,
                ax_focused_id: None,
                ocr_text: None,
                screenshot_path: None,
                screenshot_source: None,
                input_text: None,
                is_sensitive: false,
                pii_scrubbed: false,
                url: Some("https://docs.example.com/d/home/sample-document".to_string()),
                webpage_title: Some("容器云 GPU 指标采集项目 - 云文档".to_string()),
            })
            .unwrap();
        mgr.with_conn(|conn| {
            conn.execute(
                "UPDATE captures SET timeline_id = ?1 WHERE id IN (?2, ?3)",
                params![timeline_id, primary_capture_id, source_capture_id],
            )?;
            Ok(())
        })
        .unwrap();

        let candidates = mgr.list_bake_memory_init_candidates(0, 10).unwrap();
        let candidate = candidates
            .iter()
            .find(|candidate| candidate.timeline.id == timeline_id)
            .unwrap();
        assert_eq!(
            candidate.capture_url.as_deref(),
            Some("https://docs.example.com/d/home/sample-document")
        );
    }

    #[test]
    fn test_bake_candidate_prefers_repeated_member_title_over_primary_placeholder() {
        let mgr = make_mgr();
        let url = "https://docs.example.com/k/home/space/document-id";
        let primary = seed_document_capture(&mgr, 1_700_000_000_000, "正文第一页".repeat(100), url);
        let second = seed_document_capture(&mgr, 1_700_000_010_000, "正文第二页".repeat(100), url);
        let third = seed_document_capture(&mgr, 1_700_000_020_000, "正文第三页".repeat(100), url);
        let mut timeline = sample_entry(&mgr, "document");
        timeline.capture_id = primary;
        let timeline_id = mgr.insert_timeline_entry(&timeline).unwrap();
        mgr.with_conn(|conn| {
            conn.execute(
                "UPDATE captures
                 SET timeline_id = ?1, webpage_title = '知识库', win_title = '知识库'
                 WHERE id = ?2",
                params![timeline_id, primary],
            )?;
            conn.execute(
                "UPDATE captures
                 SET timeline_id = ?1,
                     webpage_title = '商业化大模型例行压测介绍 - 云文档',
                     win_title = '商业化大模型例行压测介绍 - 云文档 - Google Chrome'
                 WHERE id IN (?2, ?3)",
                params![timeline_id, second, third],
            )?;
            Ok(())
        })
        .unwrap();

        let candidate = mgr
            .list_bake_memory_init_candidates(0, 10)
            .unwrap()
            .into_iter()
            .find(|candidate| candidate.timeline.id == timeline_id)
            .unwrap();

        assert_eq!(
            candidate.preferred_source_title.as_deref(),
            Some("商业化大模型例行压测介绍 - 云文档")
        );
    }

    #[test]
    fn test_url_aggregation_keeps_longest_snapshot_when_document_head_repeats() {
        let mgr = make_mgr();
        let url = "https://docs.example.com/d/home/long-document";
        let shared_head = "共同文档开头".repeat(60);
        let short_text = format!("{shared_head}\n短版本");
        let long_tail = "长版本尾部关键信息".repeat(500);
        let long_text = format!("{shared_head}\n{long_tail}");
        seed_document_capture(&mgr, 1_700_000_000_000, short_text, url);
        seed_document_capture(&mgr, 1_700_000_010_000, long_text.clone(), url);

        let (aggregated, count) = mgr
            .with_conn(|conn| aggregate_url_capture_text(conn, url, 1_700_000_010_000))
            .unwrap()
            .expect("应返回去重后的最长文档快照");

        assert_eq!(count, 1);
        assert!(aggregated.contains(&long_tail));
        assert!(aggregated.chars().count() >= long_text.chars().count());
    }

    #[test]
    fn test_member_aggregation_keeps_longest_snapshot_even_if_only_one_remains() {
        let mgr = make_mgr();
        let url = "https://docs.example.com/d/home/member-long-document";
        let shared_head = "同一页面固定开头".repeat(60);
        let short_id = seed_document_capture(
            &mgr,
            1_700_000_000_000,
            format!("{shared_head}\n短版本"),
            url,
        );
        let long_tail = "必须保留的文档末尾".repeat(500);
        let long_text = format!("{shared_head}\n{long_tail}");
        let long_id = seed_document_capture(&mgr, 1_700_000_010_000, long_text.clone(), url);

        let (aggregated, count) = mgr
            .with_conn(|conn| aggregate_member_capture_text(conn, &[short_id, long_id], short_id))
            .unwrap()
            .expect("应保留比主 capture 更完整的单一快照");

        assert_eq!(count, 1);
        assert!(aggregated.contains(&long_tail));
        assert!(aggregated.chars().count() >= long_text.chars().count());
    }

    #[test]
    fn test_update_bake_article_details_system_works() {
        let mgr = make_mgr();
        let id = mgr
            .insert_timeline_entry(&sample_entry(&mgr, "meeting"))
            .unwrap();

        assert!(mgr
            .update_timeline_details_system(
                id,
                "更新后的情节记忆",
                Some("新的概述"),
                Some(r#"{"template_match_score":0.89,"template_match_level":"high"}"#),
                r#"["模板"]"#,
            )
            .unwrap());
    }

    #[test]
    fn test_update_bake_sop_details_system_works() {
        let mgr = make_mgr();
        let source_id = mgr
            .insert_episodic_memory(&sample_entry(&mgr, "meeting"))
            .unwrap();
        let id = mgr
            .insert_bake_sop(&NewBakeSop {
                timeline_id: source_id,
                title: "原始 SOP".to_string(),
                summary: "原始 SOP".to_string(),
                content: Some(r#"{"status":"candidate"}"#.to_string()),
                detailed_content: None,
                entities: r#"["SOP"]"#.to_string(),
                importance: 4,
                source_capture_ids: None,
            })
            .unwrap();

        assert!(mgr
            .update_timeline_details_system(
                id,
                "更新后的 SOP",
                Some("新的概述"),
                Some(r#"{"status":"candidate"}"#),
                r#"["SOP"]"#,
            )
            .unwrap());
        assert!(mgr.set_knowledge_verified(id, true).unwrap());
    }

    fn seed_unrelated_timeline(mgr: &StorageManager) -> i64 {
        let mut other = sample_entry(mgr, "bake_memory");
        other.summary = "完全无关的记录".to_string();
        other.overview = Some("无关概览".to_string());
        other.details = Some("{}".to_string());
        other.entities = r#"["无关"]"#.to_string();
        mgr.insert_timeline_entry(&other).unwrap()
    }

    fn drop_timelines_fts(mgr: &StorageManager) {
        mgr.with_conn(|conn| {
            conn.execute_batch(
                "DROP TRIGGER IF EXISTS timelines_fts_insert;
                 DROP TRIGGER IF EXISTS timelines_fts_update;
                 DROP TRIGGER IF EXISTS timelines_fts_delete;
                 DROP TABLE IF EXISTS timelines_fts;",
            )?;
            Ok(())
        })
        .unwrap();
    }

    #[test]
    fn test_list_bake_memories_query_prefilter_results_match_like() {
        let mgr = make_mgr();
        let hit_id = mgr
            .insert_timeline_entry(&sample_entry(&mgr, "bake_memory"))
            .unwrap();
        seed_unrelated_timeline(&mgr);

        let results = mgr
            .list_bake_memories_paginated(Some("客服"), None, None, 10, 0)
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, hit_id);
        assert_eq!(
            mgr.count_bake_memories_filtered(Some("客服"), None, None)
                .unwrap(),
            1
        );

        // FTS 候选接口也应命中同一批 ID
        let candidate_ids = mgr.timeline_fts_candidate_ids("客服").unwrap();
        assert!(candidate_ids.contains(&hit_id));
    }

    #[test]
    fn test_list_bake_memories_query_falls_back_without_fts() {
        let mgr = make_mgr();
        let hit_id = mgr
            .insert_timeline_entry(&sample_entry(&mgr, "bake_memory"))
            .unwrap();
        seed_unrelated_timeline(&mgr);
        drop_timelines_fts(&mgr);

        let results = mgr
            .list_bake_memories_paginated(Some("客服"), None, None, 10, 0)
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, hit_id);
        assert_eq!(
            mgr.count_bake_memories_filtered(Some("客服"), None, None)
                .unwrap(),
            1
        );

        // FTS 表缺失时候选接口返回 None，调用方回退全量过滤
        assert!(mgr.timeline_fts_candidate_ids("客服").is_none());
    }
}
