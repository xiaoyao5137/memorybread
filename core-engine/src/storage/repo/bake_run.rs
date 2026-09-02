use rusqlite::{params, Connection, OptionalExtension};

use crate::storage::{
    db::current_ts_ms,
    error::StorageError,
    models_bake::{
        BakeArtifactAuditRecord, BakeCandidateAuditRecord, BakeQueueStatusRecord,
        BakeRetryStateRecord, BakeRunRecord, BakeSopFunnelSummaryRecord, BakeWatermarkRecord,
        NewBakeArtifactAudit, NewBakeCandidateAudit, NewBakeRun,
    },
    StorageManager,
};

const STALE_RUNNING_BAKE_RUN_MS: i64 = 35 * 60 * 1000;
const RECOVERABLE_BAKE_FAILURE_PREDICATE: &str = r#"
    last_error LIKE 'upstream error (429%'
    OR last_error LIKE 'upstream error (502%'
    OR last_error LIKE 'upstream error (503%'
    OR (
        last_error LIKE 'internal error: 解析 merge_document 响应失败:%'
        AND last_error LIKE '%missing field `title`%'
    )
    OR (
        last_error LIKE 'internal error: 解析 bake knowledge payload 失败:%'
        AND last_error LIKE '%missing field `summary`%'
    )
    OR last_error LIKE 'internal error: 解析 bake sop payload 失败: invalid type:%'
    OR last_error LIKE 'internal error: 解析 bake design payload 失败: invalid type:%'
"#;

/// 把全部有效 bake_documents 的来源引用一次性展开成 timeline id 集合（TEXT 形式）。
///
/// 早期版本对每条 timeline 逐条跑 `NOT EXISTS ... json_each(bake_documents.source_*)`
/// 关联子查询，5000+ timelines × 700+ documents 会让单次查询耗时数秒；监控页
/// 3 秒级轮询反复调用后直接占满共享数据库连接。改为查询级 CTE 后 JSON 只展开
/// 一次，耗时降到几十毫秒。注意保持 TEXT 比较与旧口径严格一致。
pub const PRODUCED_DOC_TIMELINES_CTE: &str = r#"
    produced_doc_timelines AS (
        SELECT DISTINCT CAST(je.value AS TEXT) AS tid
        FROM bake_documents bd,
             json_each(CASE WHEN json_valid(COALESCE(bd.source_memory_ids, '[]'))
                            THEN bd.source_memory_ids ELSE '[]' END) je
        WHERE bd.deleted_at IS NULL
        UNION
        SELECT DISTINCT CAST(je.value AS TEXT)
        FROM bake_documents bd,
             json_each(CASE WHEN json_valid(COALESCE(bd.source_episode_ids, '[]'))
                            THEN bd.source_episode_ids ELSE '[]' END) je
        WHERE bd.deleted_at IS NULL
    )
"#;

/// 把文档成员 JSON 展开物化成连接级临时表（幂等重建）。
///
/// 捆绑 SQLite 会把仅被引用一次的 CTE 展平为关联标量子查询，外层每一行都
/// 重新跑 json_each/json_valid 展开全部文档：约 2 万 captures × 700+ 文档，
/// 单次查询 28 秒以上并长期占住共享连接，监控页因此打开要 10 秒以上。
/// 落成带索引的临时表后，关联子查询退化为普通查表，耗时回到毫秒级。
/// 口径与早期 CTE 版本严格一致：timeline 成员取 source_memory_ids，capture
/// 成员取 source_capture_ids，episode 成员取 source_episode_ids（附带文档创建
/// 时间供今日产量统计用），均只统计未删除文档。
pub(crate) fn refresh_doc_member_temp_tables(conn: &Connection) -> Result<(), StorageError> {
    conn.execute_batch(
        "DROP TABLE IF EXISTS temp.doc_member_timeline;
         DROP TABLE IF EXISTS temp.doc_member_capture;
         DROP TABLE IF EXISTS temp.doc_member_episode;
         CREATE TEMP TABLE doc_member_timeline AS
             SELECT bd.id AS doc_id, CAST(je.value AS TEXT) AS timeline_id
             FROM bake_documents bd,
                  json_each(CASE WHEN json_valid(COALESCE(bd.source_memory_ids, '[]'))
                                 THEN bd.source_memory_ids ELSE '[]' END) je
             WHERE bd.deleted_at IS NULL;
         CREATE INDEX temp.idx_doc_member_timeline_tl
             ON doc_member_timeline(timeline_id, doc_id);
         CREATE INDEX temp.idx_doc_member_timeline_doc
             ON doc_member_timeline(doc_id, timeline_id);
         CREATE TEMP TABLE doc_member_capture AS
             SELECT bd.id AS doc_id, CAST(je.value AS TEXT) AS capture_id
             FROM bake_documents bd,
                  json_each(CASE WHEN json_valid(COALESCE(bd.source_capture_ids, '[]'))
                                 THEN bd.source_capture_ids ELSE '[]' END) je
             WHERE bd.deleted_at IS NULL;
         CREATE INDEX temp.idx_doc_member_capture_pair
             ON doc_member_capture(doc_id, capture_id);
         CREATE INDEX temp.idx_doc_member_capture_cap
             ON doc_member_capture(capture_id, doc_id);
         CREATE TEMP TABLE doc_member_episode AS
             SELECT bd.id AS doc_id, CAST(je.value AS TEXT) AS episode_id,
                    bd.created_at AS doc_created_at
             FROM bake_documents bd,
                  json_each(CASE WHEN json_valid(COALESCE(bd.source_episode_ids, '[]'))
                                 THEN bd.source_episode_ids ELSE '[]' END) je
             WHERE bd.deleted_at IS NULL;
         CREATE INDEX temp.idx_doc_member_episode_ep
             ON doc_member_episode(episode_id, doc_id);",
    )
    .map_err(StorageError::Sqlite)
}

/// 一次可展示的记忆产物生产事件。
///
/// 自动提炼使用 `bake_runs` 的完成时间与产物计数；手工产物没有 bake run，
/// 因此以资产首次创建时间补入。这样既能统计“合并到既有资产”的真实收录，
/// 也不会把后台维护导致的普通 `updated_at` 刷新误算成生产。
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BakeProductionEventRecord {
    pub occurred_at_ms: i64,
    pub knowledge_count: i64,
    pub document_count: i64,
    pub sop_count: i64,
}

/// 读取指定时间之后的记忆产物生产事件。
///
/// `from_ms = 0` 用于总览历史趋势；传入本地自然日起点则用于“今日完成”。
pub fn load_bake_production_events(
    conn: &Connection,
    from_ms: i64,
) -> Result<Vec<BakeProductionEventRecord>, StorageError> {
    let mut stmt = conn.prepare(
        r#"
        WITH knowledge_assets AS (
            SELECT
                COALESCE(
                    created_at_ms,
                    CAST(strftime('%s', created_at) AS INTEGER) * 1000
                ) AS occurred_at_ms,
                COALESCE(
                    json_extract(
                        CASE WHEN json_valid(content) THEN content ELSE '{}' END,
                        '$.creation_mode'
                    ),
                    ''
                ) AS creation_mode,
                COALESCE(
                    json_extract(
                        CASE WHEN json_valid(content) THEN content ELSE '{}' END,
                        '$.generation_version'
                    ),
                    ''
                ) AS generation_version
            FROM bake_knowledge
        ),
        sop_assets AS (
            SELECT
                COALESCE(
                    created_at_ms,
                    CAST(strftime('%s', created_at) AS INTEGER) * 1000
                ) AS occurred_at_ms,
                COALESCE(
                    json_extract(
                        CASE WHEN json_valid(content) THEN content ELSE '{}' END,
                        '$.creation_mode'
                    ),
                    ''
                ) AS creation_mode,
                COALESCE(
                    json_extract(
                        CASE WHEN json_valid(content) THEN content ELSE '{}' END,
                        '$.generation_version'
                    ),
                    ''
                ) AS generation_version
            FROM bake_sops
        ),
        production_events AS (
            -- 自动提炼：run 计数包含新建和成功合并，是真实生产口径。
            SELECT
                completed_at AS occurred_at_ms,
                knowledge_created_count AS knowledge_count,
                design_created_count AS document_count,
                sop_created_count AS sop_count
            FROM bake_runs
            WHERE status = 'completed'
              AND completed_at IS NOT NULL
              AND completed_at >= ?1
              AND (
                    knowledge_created_count > 0
                 OR design_created_count > 0
                 OR sop_created_count > 0
              )

            UNION ALL

            -- 手工知识没有 bake run，按首次创建补入；旧版自动产物继续排除。
            SELECT occurred_at_ms, 1, 0, 0
            FROM knowledge_assets
            WHERE occurred_at_ms >= ?1
              AND creation_mode != 'llm_bake'
              AND NOT (
                  creation_mode = 'auto'
                  AND generation_version = 'bake-v1'
              )

            UNION ALL

            -- 手工文档同理。删除只改变当前库存，不抹除已经发生的生产历史。
            SELECT created_at, 0, 1, 0
            FROM bake_documents
            WHERE created_at >= ?1
              AND COALESCE(creation_mode, '') != 'llm_bake'
              AND NOT (
                  COALESCE(creation_mode, '') = 'auto'
                  AND COALESCE(generation_version, '') = 'bake-v1'
              )

            UNION ALL

            -- 手工操作没有 bake run，按首次创建补入。
            SELECT occurred_at_ms, 0, 0, 1
            FROM sop_assets
            WHERE occurred_at_ms >= ?1
              AND creation_mode != 'llm_bake'
              AND NOT (
                  creation_mode = 'auto'
                  AND generation_version = 'bake-v1'
              )
        )
        SELECT occurred_at_ms, knowledge_count, document_count, sop_count
        FROM production_events
        WHERE occurred_at_ms > 0
        ORDER BY occurred_at_ms ASC
        "#,
    )?;
    let rows = stmt.query_map(params![from_ms], |row| {
        Ok(BakeProductionEventRecord {
            occurred_at_ms: row.get(0)?,
            knowledge_count: row.get(1)?,
            document_count: row.get(2)?,
            sop_count: row.get(3)?,
        })
    })?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(StorageError::Sqlite)
}

impl StorageManager {
    pub fn count_pending_operation_replays(&self, max_failures: i64) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT COUNT(*)
                 FROM operation_replay_queue oq
                 JOIN timelines t ON t.id = oq.timeline_id
                 LEFT JOIN bake_retry_state r ON r.timeline_id = t.id
                 WHERE (
                       oq.status = 'pending'
                       OR (
                           oq.status = 'claimed'
                           AND COALESCE(oq.claimed_at_ms, 0) <= ?1
                       )
                 )
                   AND t.category NOT IN (
                       'bake_article', 'bake_knowledge', 'bake_sop', 'legacy_bake_candidate'
                   )
                   AND COALESCE(r.failure_count, 0) < ?2
                   AND NOT EXISTS (
                       SELECT 1 FROM bake_sops bs WHERE bs.timeline_id = t.id
                   )",
                params![current_ts_ms().saturating_sub(30 * 60 * 1000), max_failures],
                |row| row.get(0),
            )
            .map_err(StorageError::from)
        })
    }

    pub fn claim_operation_replay(
        &self,
        timeline_id: i64,
        run_id: i64,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE operation_replay_queue
                 SET status = 'claimed', claimed_at_ms = ?3, last_run_id = ?2
                 WHERE timeline_id = ?1
                   AND (
                       status = 'pending'
                       OR (status = 'claimed' AND COALESCE(claimed_at_ms, 0) <= ?4)
                   )",
                params![
                    timeline_id,
                    run_id,
                    current_ts_ms(),
                    current_ts_ms().saturating_sub(30 * 60 * 1000)
                ],
            )?;
            Ok(affected > 0)
        })
    }

    pub fn finish_operation_replay(
        &self,
        timeline_id: i64,
        status: &str,
    ) -> Result<(), StorageError> {
        debug_assert!(matches!(status, "completed" | "discarded" | "pending"));
        self.with_conn(|conn| {
            let now = current_ts_ms();
            conn.execute(
                "UPDATE operation_replay_queue
                 SET status = ?2,
                     completed_at_ms = CASE WHEN ?2 IN ('completed', 'discarded') THEN ?3 ELSE NULL END,
                     claimed_at_ms = CASE WHEN ?2 = 'pending' THEN NULL ELSE claimed_at_ms END
                 WHERE timeline_id = ?1",
                params![timeline_id, status, now],
            )?;
            Ok(())
        })
    }

    /// 写入候选的确定性预检状态。审计表只保存计数、分类和原因码，不保存候选正文。
    pub fn upsert_bake_candidate_audit(
        &self,
        audit: &NewBakeCandidateAudit,
    ) -> Result<(), StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO bake_candidate_audits (
                    run_id, timeline_id, lane, source_capture_count,
                    effective_capture_count, sop_eligible, sop_eligibility_state, sop_eligibility_reason,
                    sop_evidence_mode, persist_status, persist_reason, created_at_ms, updated_at_ms
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?12)
                 ON CONFLICT(run_id, timeline_id) DO UPDATE SET
                    lane = excluded.lane,
                    source_capture_count = excluded.source_capture_count,
                    effective_capture_count = excluded.effective_capture_count,
                    sop_eligible = excluded.sop_eligible,
                    sop_eligibility_state = excluded.sop_eligibility_state,
                    sop_eligibility_reason = excluded.sop_eligibility_reason,
                    sop_evidence_mode = excluded.sop_evidence_mode,
                    persist_status = excluded.persist_status,
                    persist_reason = excluded.persist_reason,
                    updated_at_ms = excluded.updated_at_ms",
                params![
                    audit.run_id,
                    audit.timeline_id,
                    audit.lane,
                    audit.source_capture_count,
                    audit.effective_capture_count,
                    audit.sop_eligible,
                    audit.sop_eligibility_state,
                    audit.sop_eligibility_reason,
                    audit.sop_evidence_mode,
                    audit.persist_status,
                    audit.persist_reason,
                    now,
                ],
            )?;
            Ok(())
        })
    }

    pub fn update_bake_candidate_audit_model(
        &self,
        run_id: i64,
        timeline_id: i64,
        primary_type: Option<&str>,
        classification_reason: Option<&str>,
        sop_model_accepted: bool,
        sop_model_reason: Option<&str>,
        sop_payload_valid: Option<bool>,
    ) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE bake_candidate_audits
                 SET primary_type = ?3,
                     classification_reason = ?4,
                     sop_model_accepted = ?5,
                     sop_model_reason = ?6,
                     sop_payload_valid = ?7,
                     persist_status = 'extracted',
                     updated_at_ms = ?8
                 WHERE run_id = ?1 AND timeline_id = ?2",
                params![
                    run_id,
                    timeline_id,
                    primary_type,
                    classification_reason,
                    sop_model_accepted,
                    sop_model_reason,
                    sop_payload_valid,
                    current_ts_ms(),
                ],
            )?;
            Ok(())
        })
    }

    pub fn finalize_bake_candidate_audit(
        &self,
        run_id: i64,
        timeline_id: i64,
        persist_status: &str,
        persist_reason: Option<&str>,
    ) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE bake_candidate_audits
                 SET persist_status = ?3, persist_reason = ?4, updated_at_ms = ?5
                 WHERE run_id = ?1 AND timeline_id = ?2",
                params![
                    run_id,
                    timeline_id,
                    persist_status,
                    persist_reason,
                    current_ts_ms(),
                ],
            )?;
            Ok(())
        })
    }

    pub fn get_bake_candidate_audit(
        &self,
        run_id: i64,
        timeline_id: i64,
    ) -> Result<Option<BakeCandidateAuditRecord>, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT id, run_id, timeline_id, lane, source_capture_count,
                        effective_capture_count, sop_eligible, sop_eligibility_state, sop_eligibility_reason,
                        sop_evidence_mode, primary_type, classification_reason, sop_model_accepted,
                        sop_model_reason, sop_payload_valid, persist_status, persist_reason,
                        created_at_ms, updated_at_ms
                 FROM bake_candidate_audits
                 WHERE run_id = ?1 AND timeline_id = ?2",
                params![run_id, timeline_id],
                row_to_bake_candidate_audit,
            )
            .optional()
            .map_err(StorageError::from)
        })
    }

    pub fn upsert_bake_artifact_audit(
        &self,
        audit: &NewBakeArtifactAudit,
    ) -> Result<(), StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO bake_artifact_audits (
                    run_id, timeline_id, artifact_kind, deterministic_eligible,
                    deterministic_reason, model_accepted, model_reason, payload_present,
                    payload_valid, artifact_shape, compatibility_recovered,
                    persist_status, created_at_ms, updated_at_ms
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'extracted', ?12, ?12)
                 ON CONFLICT(run_id, timeline_id, artifact_kind) DO UPDATE SET
                    deterministic_eligible = excluded.deterministic_eligible,
                    deterministic_reason = excluded.deterministic_reason,
                    model_accepted = excluded.model_accepted,
                    model_reason = excluded.model_reason,
                    payload_present = excluded.payload_present,
                    payload_valid = excluded.payload_valid,
                    artifact_shape = excluded.artifact_shape,
                    compatibility_recovered = excluded.compatibility_recovered,
                    persist_status = 'extracted',
                    persist_reason = NULL,
                    artifact_id = NULL,
                    decision_state = NULL,
                    quality_score = NULL,
                    decision_reason_code = NULL,
                    decision_reason_summary = NULL,
                    decision_rule_version = NULL,
                    shadow_payload_json = NULL,
                    updated_at_ms = excluded.updated_at_ms",
                params![
                    audit.run_id,
                    audit.timeline_id,
                    audit.artifact_kind,
                    audit.deterministic_eligible,
                    audit.deterministic_reason,
                    audit.model_accepted,
                    audit.model_reason,
                    audit.payload_present,
                    audit.payload_valid,
                    audit.artifact_shape,
                    audit.compatibility_recovered,
                    now,
                ],
            )?;
            Ok(())
        })
    }

    pub fn finalize_bake_artifact_audit(
        &self,
        run_id: i64,
        timeline_id: i64,
        artifact_kind: &str,
        persist_status: &str,
        persist_reason: Option<&str>,
        artifact_id: Option<i64>,
    ) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE bake_artifact_audits
                 SET persist_status = ?4, persist_reason = ?5, artifact_id = ?6, updated_at_ms = ?7
                 WHERE run_id = ?1 AND timeline_id = ?2 AND artifact_kind = ?3",
                params![
                    run_id,
                    timeline_id,
                    artifact_kind,
                    persist_status,
                    persist_reason,
                    artifact_id,
                    current_ts_ms(),
                ],
            )?;
            Ok(())
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn finalize_bake_artifact_audit_decision(
        &self,
        run_id: i64,
        timeline_id: i64,
        artifact_kind: &str,
        persist_status: &str,
        persist_reason: Option<&str>,
        artifact_id: Option<i64>,
        decision_state: Option<&str>,
        quality_score: Option<f64>,
        decision_reason_code: Option<&str>,
        decision_reason_summary: Option<&str>,
        decision_rule_version: Option<&str>,
        shadow_payload_json: Option<&str>,
    ) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE bake_artifact_audits
                 SET persist_status = ?4, persist_reason = ?5, artifact_id = ?6,
                     decision_state = ?7, quality_score = ?8,
                     decision_reason_code = ?9, decision_reason_summary = ?10,
                     decision_rule_version = ?11, shadow_payload_json = ?12,
                     updated_at_ms = ?13
                 WHERE run_id = ?1 AND timeline_id = ?2 AND artifact_kind = ?3",
                params![
                    run_id,
                    timeline_id,
                    artifact_kind,
                    persist_status,
                    persist_reason,
                    artifact_id,
                    decision_state,
                    quality_score,
                    decision_reason_code,
                    decision_reason_summary,
                    decision_rule_version,
                    shadow_payload_json,
                    current_ts_ms(),
                ],
            )?;
            Ok(())
        })
    }

    pub fn list_bake_artifact_audits_for_timeline(
        &self,
        timeline_id: i64,
        limit: usize,
    ) -> Result<Vec<BakeArtifactAuditRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, run_id, timeline_id, artifact_kind, deterministic_eligible,
                        deterministic_reason, model_accepted, model_reason, payload_present,
                        payload_valid, artifact_shape, compatibility_recovered, persist_status,
                        persist_reason, artifact_id, decision_state, quality_score,
                        decision_reason_code, decision_reason_summary, decision_rule_version,
                        shadow_payload_json, created_at_ms, updated_at_ms
                 FROM bake_artifact_audits
                 WHERE timeline_id = ?1
                 ORDER BY created_at_ms DESC, id DESC
                 LIMIT ?2",
            )?;
            let rows = stmt.query_map(
                params![timeline_id, limit.min(100) as i64],
                row_to_bake_artifact_audit,
            )?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::from)
        })
    }

    pub fn get_bake_run_sop_funnel_summary(
        &self,
        run_id: i64,
    ) -> Result<BakeSopFunnelSummaryRecord, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT COUNT(*),
                        COALESCE(SUM(sop_eligible = 1), 0),
                        COALESCE(SUM(sop_model_accepted = 1), 0),
                        COALESCE(SUM(sop_payload_valid = 1), 0),
                        COALESCE(SUM(persist_status = 'created'), 0)
                 FROM bake_candidate_audits
                 WHERE run_id = ?1",
                params![run_id],
                |row| {
                    Ok(BakeSopFunnelSummaryRecord {
                        audited_count: row.get(0)?,
                        eligible_count: row.get(1)?,
                        model_accepted_count: row.get(2)?,
                        payload_valid_count: row.get(3)?,
                        persisted_count: row.get(4)?,
                    })
                },
            )
            .map_err(StorageError::from)
        })
    }

    pub fn insert_bake_run(&self, run: &NewBakeRun) -> Result<i64, StorageError> {
        self.with_conn(|conn| insert_bake_run_inner(conn, run))
    }

    pub fn get_latest_bake_run(&self) -> Result<Option<BakeRunRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT id, trigger_reason, status, started_at, completed_at,
                        processed_episode_count, auto_created_count, candidate_count, discarded_count,
                        knowledge_created_count, design_created_count, sop_created_count,
                        error_message, latency_ms
                 FROM bake_runs
                 ORDER BY started_at DESC, id DESC
                 LIMIT 1",
            )?;
            let mut rows = stmt.query([])?;
            if let Some(row) = rows.next()? {
                Ok(Some(row_to_bake_run(row)?))
            } else {
                Ok(None)
            }
        })
    }

    /// 返回当前处于 running 状态的 bake run 数量。
    /// 用于并发保护：限制同时运行的 bake run 数量，避免过多 run 竞争 sidecar LLM。
    pub fn count_running_bake_runs(&self) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let fresh_after_ms = current_ts_ms() - STALE_RUNNING_BAKE_RUN_MS;
            let count: i64 = conn.query_row(
                "SELECT COUNT(*) FROM bake_runs
                 WHERE status = 'running'
                   AND started_at >= ?1",
                params![fresh_after_ms],
                |row| row.get(0),
            )?;
            Ok(count)
        })
    }

    /// 进程启动时收敛所有遗留 running run。
    ///
    /// bake worker 只存在于进程内且没有持久化租约；新进程不可能接管旧 run。
    /// 因此启动时无论 started_at 多新，数据库中的 running 都一定是孤儿。
    pub fn fail_orphaned_running_bake_runs_on_startup(&self) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let affected = conn.execute(
                "UPDATE bake_runs
                 SET status = 'failed',
                     completed_at = COALESCE(completed_at, ?1),
                     error_message = CASE
                         WHEN COALESCE(error_message, '') = ''
                         THEN 'orphaned running bake run recovered on startup'
                         ELSE error_message
                     END,
                     latency_ms = COALESCE(latency_ms, MAX(0, ?1 - started_at))
                 WHERE status = 'running'",
                params![now],
            )?;
            Ok(affected as i64)
        })
    }

    /// 将已超过正常批次上限、但因进程退出未写终态的 run 收敛为 failed。
    ///
    /// 启动恢复存在一个窗口：进程重启时未满 35 分钟的遗留 run 当时仍算 fresh，
    /// 之后才会变 stale。因此除了启动时调用，运行期触发和监控也需要按需调用。
    pub fn fail_stale_running_bake_runs(&self) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            let now = current_ts_ms();
            let fresh_after_ms = now - STALE_RUNNING_BAKE_RUN_MS;
            let affected = conn.execute(
                "UPDATE bake_runs
                 SET status = 'failed',
                     completed_at = COALESCE(completed_at, ?1),
                     error_message = CASE
                         WHEN COALESCE(error_message, '') = ''
                         THEN 'stale running bake run reconciled after runtime limit'
                         ELSE error_message
                     END,
                     latency_ms = COALESCE(latency_ms, MAX(0, ?1 - started_at))
                 WHERE status = 'running'
                   AND started_at < ?2",
                params![now, fresh_after_ms],
            )?;
            Ok(affected as i64)
        })
    }

    /// 更新 bake run 的实时进度字段（candidate_count / processed_episode_count），
    /// 用于监控页实时展示"提炼中"数量，不改变 run 状态。
    pub fn update_bake_run_progress(
        &self,
        id: i64,
        candidate_count: i64,
        processed_episode_count: i64,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_runs
                 SET candidate_count = ?1,
                     processed_episode_count = ?2
                 WHERE id = ?3
                   AND status = 'running'",
                params![candidate_count, processed_episode_count, id],
            )?;
            Ok(affected > 0)
        })
    }

    /// 将 run 标记为失败，但保留已实时写入的候选数和处理进度。
    ///
    /// 一个批次可能在成功持久化若干候选后被 P0 抢占；若用全 0 覆盖终态，
    /// 监控会错误显示本轮毫无进展。
    pub fn fail_bake_run_preserving_progress(
        &self,
        id: i64,
        completed_at: i64,
        error_message: &str,
        latency_ms: Option<i64>,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_runs
                 SET status = 'failed',
                     completed_at = ?1,
                     error_message = ?2,
                     latency_ms = ?3
                 WHERE id = ?4
                   AND status = 'running'",
                params![completed_at, error_message, latency_ms, id],
            )?;
            Ok(affected > 0)
        })
    }

    /// 上游资源竞争或 P0 抢占时把 run 标记为 deferred，并保留已落盘进度。
    ///
    /// deferred 不是内容失败；watermark 会停在尚未完成的候选之前，下一轮从该
    /// 候选继续，不增加 bake_retry_state 的失败次数。
    pub fn defer_bake_run_preserving_progress(
        &self,
        id: i64,
        completed_at: i64,
        reason: &str,
        latency_ms: Option<i64>,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_runs
                 SET status = 'deferred',
                     completed_at = ?1,
                     error_message = ?2,
                     latency_ms = ?3
                 WHERE id = ?4
                   AND status = 'running'",
                params![completed_at, reason, latency_ms, id],
            )?;
            Ok(affected > 0)
        })
    }

    #[allow(clippy::too_many_arguments)]
    pub fn complete_bake_run(
        &self,
        id: i64,
        status: &str,
        completed_at: i64,
        processed_episode_count: i64,
        auto_created_count: i64,
        candidate_count: i64,
        discarded_count: i64,
        knowledge_created_count: i64,
        document_created_count: i64,
        sop_created_count: i64,
        error_message: Option<&str>,
        latency_ms: Option<i64>,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let affected = conn.execute(
                "UPDATE bake_runs
                 SET status = ?1,
                     completed_at = ?2,
                     processed_episode_count = ?3,
                     auto_created_count = ?4,
                     candidate_count = ?5,
                     discarded_count = ?6,
                     knowledge_created_count = ?7,
                     design_created_count = ?8,
                     sop_created_count = ?9,
                     error_message = ?10,
                     latency_ms = ?11
                 WHERE id = ?12
                   AND status = 'running'",
                params![
                    status,
                    completed_at,
                    processed_episode_count,
                    auto_created_count,
                    candidate_count,
                    discarded_count,
                    knowledge_created_count,
                    document_created_count,
                    sop_created_count,
                    error_message,
                    latency_ms,
                    id,
                ],
            )?;
            Ok(affected > 0)
        })
    }

    /// 记录触发时刻读到的队列 actionable 口径，供监控核对“状态说有待烘、run 却选不出候选”的口径漂移。
    pub fn set_bake_run_trigger_actionable_count(
        &self,
        id: i64,
        trigger_actionable_count: i64,
    ) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute(
                "UPDATE bake_runs SET trigger_actionable_count = ?1 WHERE id = ?2",
                params![trigger_actionable_count, id],
            )?;
            Ok(())
        })
    }

    /// 读取 run 触发时刻记录的队列 actionable 口径；尚未记录时为 None。
    pub fn get_bake_run_trigger_actionable_count(
        &self,
        id: i64,
    ) -> Result<Option<i64>, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT trigger_actionable_count FROM bake_runs WHERE id = ?1",
                params![id],
                |row| row.get(0),
            )
            .optional()
            .map_err(StorageError::Sqlite)
        })
    }

    pub fn get_bake_watermark(
        &self,
        pipeline_name: &str,
    ) -> Result<Option<BakeWatermarkRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT pipeline_name, last_processed_ts, updated_at
                 FROM bake_watermarks
                 WHERE pipeline_name = ?1",
            )?;
            let mut rows = stmt.query(params![pipeline_name])?;
            if let Some(row) = rows.next()? {
                Ok(Some(row_to_bake_watermark(row)?))
            } else {
                Ok(None)
            }
        })
    }

    pub fn upsert_bake_watermark(
        &self,
        pipeline_name: &str,
        last_processed_ts: i64,
    ) -> Result<(), StorageError> {
        let updated_at = current_ts_ms();
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO bake_watermarks (pipeline_name, last_processed_ts, updated_at)
                 VALUES (?1, ?2, ?3)
                 ON CONFLICT(pipeline_name) DO UPDATE SET
                     last_processed_ts = excluded.last_processed_ts,
                     updated_at = excluded.updated_at",
                params![pipeline_name, last_processed_ts, updated_at],
            )?;
            Ok(())
        })
    }

    /// 记录单条 timeline 的烘焙失败。
    ///
    /// failure_count 由 BakeService 用于有界重试；达到服务端上限后才成为终态。
    pub fn bump_bake_retry_failure(
        &self,
        timeline_id: i64,
        last_error: &str,
    ) -> Result<i64, StorageError> {
        self.bump_bake_retry_failure_with_code(timeline_id, last_error, "BAKE_UNKNOWN")
    }

    /// 记录候选失败，并把下一次可执行时间持久化到数据库。
    ///
    /// 退避按错误类型区分；即使 Core 或 Sidecar 重启，也不会把 502/超时候选
    /// 立即重新塞回模型队列。
    pub fn bump_bake_retry_failure_with_code(
        &self,
        timeline_id: i64,
        last_error: &str,
        error_code: &str,
    ) -> Result<i64, StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            let previous_count: i64 = conn.query_row(
                "SELECT COALESCE(
                    (SELECT failure_count FROM bake_retry_state WHERE timeline_id = ?1),
                    0
                 )",
                params![timeline_id],
                |row| row.get(0),
            )?;
            let next_count = previous_count.saturating_add(1);
            let next_retry_at_ms =
                now.saturating_add(bake_retry_delay_ms(error_code, next_count, timeline_id));
            conn.execute(
                "INSERT INTO bake_retry_state (
                     timeline_id, failure_count, last_error, last_failed_at_ms,
                     last_error_code, next_retry_at_ms
                 )
                 VALUES (?1, 1, ?2, ?3, ?4, ?5)
                 ON CONFLICT(timeline_id) DO UPDATE SET
                     failure_count = bake_retry_state.failure_count + 1,
                     last_error = excluded.last_error,
                     last_failed_at_ms = excluded.last_failed_at_ms,
                     last_error_code = excluded.last_error_code,
                     next_retry_at_ms = excluded.next_retry_at_ms",
                params![timeline_id, last_error, now, error_code, next_retry_at_ms],
            )?;
            let count: i64 = conn.query_row(
                "SELECT failure_count FROM bake_retry_state WHERE timeline_id = ?1",
                params![timeline_id],
                |r| r.get(0),
            )?;
            Ok(count)
        })
    }

    pub fn get_bake_retry_state(
        &self,
        timeline_id: i64,
    ) -> Result<Option<BakeRetryStateRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT timeline_id, failure_count, last_error, last_error_code,
                        last_failed_at_ms, next_retry_at_ms
                 FROM bake_retry_state
                 WHERE timeline_id = ?1",
            )?;
            let mut rows = stmt.query(params![timeline_id])?;
            if let Some(row) = rows.next()? {
                Ok(Some(BakeRetryStateRecord {
                    timeline_id: row.get(0)?,
                    failure_count: row.get(1)?,
                    last_error: row.get(2)?,
                    last_error_code: row.get(3)?,
                    last_failed_at_ms: row.get(4)?,
                    next_retry_at_ms: row.get(5)?,
                }))
            } else {
                Ok(None)
            }
        })
    }

    /// 返回候选已记录的失败次数；尚未失败时为 0。
    pub fn get_bake_retry_failure_count(&self, timeline_id: i64) -> Result<i64, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT COALESCE(
                    (SELECT failure_count FROM bake_retry_state WHERE timeline_id = ?1),
                    0
                 )",
                params![timeline_id],
                |row| row.get(0),
            )
            .map_err(StorageError::from)
        })
    }

    /// 候选成功处理后移除旧失败记录，避免历史失败继续污染监控或后续增量处理。
    pub fn clear_bake_retry_failure(&self, timeline_id: i64) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let changed = conn.execute(
                "DELETE FROM bake_retry_state WHERE timeline_id = ?1",
                params![timeline_id],
            )?;
            Ok(changed > 0)
        })
    }

    /// 返回调度与监控共用的烘焙队列快照。候选价值、watermark、产物排除、
    /// retry due/dead-letter 口径在此集中，避免 Sidecar 和监控各自复制 SQL。
    pub fn get_bake_queue_status(
        &self,
        max_failures: i64,
    ) -> Result<BakeQueueStatusRecord, StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            let mut status = conn.query_row(
                &format!(
                    r#"
                WITH wm AS (
                    SELECT
                        COALESCE(MAX(last_processed_ts), 0) AS last_processed_ts,
                        MAX(updated_at) AS updated_at
                    FROM bake_watermarks
                    WHERE pipeline_name = 'unified'
                ),
                {produced_doc_timelines_cte},
                queue AS (
                    SELECT
                        t.id,
                        MAX(
                            COALESCE(t.updated_at_ms, 0),
                            COALESCE((SELECT MAX(c2.ts) FROM captures c2 WHERE c2.timeline_id = t.id), 0)
                        ) AS candidate_ts,
                        COALESCE(r.failure_count, 0) AS failure_count,
                        COALESCE(r.next_retry_at_ms, 0) AS next_retry_at_ms,
                        COALESCE(r.last_error_code, '') AS last_error_code,
                        wm.last_processed_ts AS watermark_ts,
                        wm.updated_at AS watermark_updated_at_ms
                    FROM timelines t
                    CROSS JOIN wm
                    LEFT JOIN bake_retry_state r ON r.timeline_id = t.id
                    WHERE t.category NOT IN (
                        'bake_article', 'bake_knowledge', 'bake_sop', 'legacy_bake_candidate'
                    )
                      AND t.is_self_generated = 0
                      AND (
                            t.importance >= 4
                         OR t.user_verified = 1
                         OR t.history_view = 1
                         OR (
                                t.evidence_strength IN ('high', 'medium')
                            AND (
                                   t.activity_type IN ('coding', 'reading', 'reviewing_history', 'document_reference')
                                OR t.content_origin IN ('historical_content', 'live_interaction')
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
                              AND LENGTH(REPLACE(REPLACE(
                                  COALESCE(dc.ax_text, '') || COALESCE(dc.ocr_text, ''),
                                  ' ', ''), char(10), '')) >= 200
                         )
                      )
                      AND NOT EXISTS (SELECT 1 FROM bake_knowledge bk WHERE bk.timeline_id = t.id)
                      AND NOT EXISTS (SELECT 1 FROM bake_sops bs WHERE bs.timeline_id = t.id)
                      AND CAST(t.id AS TEXT) NOT IN (SELECT tid FROM produced_doc_timelines)
                )
                SELECT
                    COALESCE(MAX(watermark_ts), 0),
                    MAX(watermark_updated_at_ms),
                    COALESCE(SUM(failure_count = 0 AND candidate_ts > watermark_ts), 0),
                    COALESCE(SUM(failure_count > 0 AND failure_count < ?1 AND next_retry_at_ms <= ?2), 0),
                    COALESCE(SUM(failure_count > 0 AND failure_count < ?1 AND next_retry_at_ms > ?2), 0),
                    COALESCE(SUM(failure_count >= ?1), 0),
                    COALESCE(SUM(failure_count > 0 AND last_error_code IN ('INFERENCE_TIMEOUT', 'GATEWAY_TIMEOUT')), 0),
                    COALESCE(SUM(failure_count > 0 AND last_error_code IN (
                        'BAKE_OUTPUT_TRUNCATED', 'BAKE_OUTPUT_INVALID', 'BAKE_MODEL_RESPONSE_INVALID'
                    )), 0),
                    COALESCE(SUM(failure_count > 0 AND last_error_code IN (
                        'BAKE_MODEL_UPSTREAM_ERROR', 'BAKE_UNCLASSIFIED_UPSTREAM_ERROR'
                    )), 0),
                    COALESCE(SUM(failure_count > 0 AND last_error_code NOT IN (
                        'INFERENCE_TIMEOUT', 'GATEWAY_TIMEOUT', 'BAKE_OUTPUT_TRUNCATED',
                        'BAKE_OUTPUT_INVALID', 'BAKE_MODEL_RESPONSE_INVALID',
                        'BAKE_MODEL_UPSTREAM_ERROR', 'BAKE_UNCLASSIFIED_UPSTREAM_ERROR'
                    )), 0),
                    MIN(CASE WHEN failure_count = 0 AND candidate_ts > watermark_ts THEN candidate_ts END),
                    MIN(CASE WHEN failure_count > 0 AND failure_count < ?1
                        THEN candidate_ts END),
                    MIN(CASE WHEN
                        (failure_count = 0 AND candidate_ts > watermark_ts)
                        OR (failure_count > 0 AND failure_count < ?1 AND next_retry_at_ms <= ?2)
                        THEN candidate_ts END),
                    MIN(CASE WHEN failure_count > 0 AND failure_count < ?1 AND next_retry_at_ms > ?2
                        THEN next_retry_at_ms END)
                FROM queue
                "#,
                    produced_doc_timelines_cte = PRODUCED_DOC_TIMELINES_CTE
                ),
                params![max_failures, now],
                |row| {
                    let fresh_count: i64 = row.get(2)?;
                    let retry_ready_count: i64 = row.get(3)?;
                    let retry_delayed_count: i64 = row.get(4)?;
                    let dead_letter_count: i64 = row.get(5)?;
                    Ok(BakeQueueStatusRecord {
                        watermark_last_processed_ts: row.get(0)?,
                        watermark_updated_at_ms: row.get(1)?,
                        fresh_count,
                        metadata_refresh_count: 0,
                        operation_replay_count: 0,
                        retry_ready_count,
                        retry_delayed_count,
                        dead_letter_count,
                        retry_timeout_count: row.get(6)?,
                        retry_output_count: row.get(7)?,
                        retry_upstream_count: row.get(8)?,
                        retry_other_count: row.get(9)?,
                        actionable_count: fresh_count.saturating_add(retry_ready_count),
                        // 等待队列只统计仍会被调度的候选。已耗尽重试的终态失败
                        // 单独通过 dead_letter_count 告警，不能让“等待”永远清不零。
                        pending_count: fresh_count
                            .saturating_add(retry_ready_count)
                            .saturating_add(retry_delayed_count),
                        oldest_fresh_at_ms: row.get(10)?,
                        oldest_retry_at_ms: row.get(11)?,
                        oldest_actionable_at_ms: row.get(12)?,
                        next_retry_at_ms: row.get(13)?,
                        recent_no_progress_count: 0,
                        recommended_retry_after_ms: 0,
                    })
                },
            )?;

            let (watermark_ts, watermark_updated_at_ms) = conn
                .query_row(
                    "SELECT COALESCE(MAX(last_processed_ts), 0), MAX(updated_at)
                     FROM bake_watermarks
                     WHERE pipeline_name = 'unified'",
                    [],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .unwrap_or((0, None));
            status.watermark_last_processed_ts = watermark_ts;
            status.watermark_updated_at_ms = watermark_updated_at_ms;
            // 文档成员必须先物化成临时表：捆绑 SQLite 会把只引用一次的 CTE
            // 展平为关联子查询，外层每行重跑 json_each，单次要数十秒。
            refresh_doc_member_temp_tables(conn)?;
            status.metadata_refresh_count = conn
                .query_row(
                    r#"
                    SELECT COUNT(DISTINCT t.id)
                    FROM timelines t
                    JOIN doc_member_timeline dm ON dm.timeline_id = CAST(t.id AS TEXT)
                    JOIN captures c ON c.timeline_id = t.id
                    LEFT JOIN bake_retry_state r ON r.timeline_id = t.id
                    -- 口径必须与 run 时候选选择一致：failure_count > 0 的 timeline
                    -- 进不了 fresh lane，而已有文档引用又让它进不了 retry lane，
                    -- 数进 actionable 只会让触发方永远空转（no_op 活锁）。
                    WHERE COALESCE(r.failure_count, 0) = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM doc_member_capture dc
                          WHERE dc.doc_id = dm.doc_id
                            AND dc.capture_id = CAST(c.id AS TEXT)
                      )
                    "#,
                    [],
                    |row| row.get(0),
                )
                .unwrap_or(0);
            status.operation_replay_count = conn
                .query_row(
                    "SELECT COUNT(*)
                     FROM operation_replay_queue oq
                     JOIN timelines t ON t.id = oq.timeline_id
                     LEFT JOIN bake_retry_state r ON r.timeline_id = t.id
                     WHERE (
                           oq.status = 'pending'
                           OR (
                               oq.status = 'claimed'
                               AND COALESCE(oq.claimed_at_ms, 0) <= ?1
                           )
                     )
                       AND t.category NOT IN (
                           'bake_article', 'bake_knowledge', 'bake_sop', 'legacy_bake_candidate'
                       )
                       AND COALESCE(r.failure_count, 0) < ?2
                       AND NOT EXISTS (
                           SELECT 1 FROM bake_sops bs WHERE bs.timeline_id = t.id
                       )",
                    params![now.saturating_sub(30 * 60 * 1000), max_failures],
                    |row| row.get(0),
                )
                .unwrap_or(0);
            status.actionable_count = status
                .actionable_count
                .saturating_add(status.metadata_refresh_count)
                .saturating_add(status.operation_replay_count);
            status.pending_count = status
                .pending_count
                .saturating_add(status.operation_replay_count);

            // 只读取最近 5 条并计算“从最新一条开始连续”的 no_op 次数。
            // completed 空产物批次可能只是跳过低价值候选并推进了 watermark，
            // 不能算无进展；中间出现一次真实进展也必须立即打断连续计数。
            let (recent_no_progress_count, latest_no_progress_at_ms) = (|| {
                let mut stmt = conn.prepare(
                    "SELECT status, completed_at
                     FROM bake_runs
                     ORDER BY started_at DESC, id DESC
                     LIMIT 5",
                )?;
                let rows = stmt.query_map([], |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, Option<i64>>(1)?))
                })?;
                let mut count = 0_i64;
                let mut latest_completed_at = None;
                for row in rows {
                    let (run_status, completed_at) = row?;
                    if run_status != "no_op" {
                        break;
                    }
                    if latest_completed_at.is_none() {
                        latest_completed_at = completed_at;
                    }
                    count += 1;
                }
                Ok::<_, rusqlite::Error>((count, latest_completed_at))
            })()
            .unwrap_or((0, None));
            status.recent_no_progress_count = recent_no_progress_count;
            status.recommended_retry_after_ms = if status.actionable_count == 0 {
                status
                    .next_retry_at_ms
                    .map(|next| next.saturating_sub(now).clamp(1_000, 300_000))
                    .unwrap_or(300_000)
            } else if status.recent_no_progress_count > 0 {
                let backoff_ms = 15_000_i64
                    .saturating_mul(1_i64 << status.recent_no_progress_count.min(4))
                    .min(300_000);
                latest_no_progress_at_ms
                    .map(|completed_at| {
                        completed_at
                            .saturating_add(backoff_ms)
                            .saturating_sub(now)
                            .clamp(0, backoff_ms)
                    })
                    .unwrap_or(0)
            } else {
                0
            };
            Ok(status)
        })
    }

    /// 恢复由资源竞争或旧兼容性问题造成的历史失败记录。
    ///
    /// 删除误写的死信之前先找到最早受影响候选，并把 unified watermark
    /// 回退到它之前。否则只删失败标记仍会因为 watermark 已跨过候选而无法补偿。
    /// 新错误会以 `bake_error code=... status=...` 保存，不匹配这组仅用于
    /// 一次性历史修复的旧字符串，避免每次重启都把有界重试计数清零。
    pub fn clear_recoverable_bake_retry_failures(&self) -> Result<usize, StorageError> {
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            let earliest_candidate_ts = tx.query_row(
                &format!(
                    "SELECT MIN(
                        MAX(
                            COALESCE(t.updated_at_ms, 0),
                            COALESCE(
                                (SELECT MAX(c.ts) FROM captures c WHERE c.timeline_id = t.id),
                                0
                            )
                        )
                     )
                     FROM bake_retry_state r
                     JOIN timelines t ON t.id = r.timeline_id
                     WHERE {}",
                    RECOVERABLE_BAKE_FAILURE_PREDICATE
                ),
                [],
                |row| row.get::<_, Option<i64>>(0),
            )?;
            let changed = tx.execute(
                &format!(
                    "DELETE FROM bake_retry_state WHERE {}",
                    RECOVERABLE_BAKE_FAILURE_PREDICATE
                ),
                [],
            )?;
            if changed > 0 {
                if let Some(earliest) = earliest_candidate_ts {
                    let resume_before = earliest.saturating_sub(1).max(0);
                    tx.execute(
                        "UPDATE bake_watermarks
                         SET last_processed_ts = MIN(last_processed_ts, ?1),
                             updated_at = ?2
                         WHERE pipeline_name = 'unified'",
                        params![resume_before, current_ts_ms()],
                    )?;
                }
            }
            tx.commit()?;
            Ok(changed)
        })
    }
}

fn bake_retry_delay_ms(error_code: &str, failure_count: i64, timeline_id: i64) -> i64 {
    let attempt = failure_count.clamp(1, 8) as u32;
    match error_code {
        "BAKE_OUTPUT_TRUNCATED" | "BAKE_OUTPUT_INVALID" | "BAKE_MODEL_RESPONSE_INVALID" => {
            match attempt {
                1 => 10_000,
                2 => 30_000,
                _ => 60_000,
            }
        }
        "INFERENCE_TIMEOUT" | "GATEWAY_TIMEOUT" => match attempt {
            1 => 60_000,
            2 => 120_000,
            _ => 300_000,
        },
        "BAKE_MODEL_UPSTREAM_ERROR" | "BAKE_UNCLASSIFIED_UPSTREAM_ERROR" => {
            let base = 300_000_i64
                .saturating_mul(1_i64.checked_shl(attempt.saturating_sub(1)).unwrap_or(8))
                .min(1_800_000);
            let jitter = timeline_id.unsigned_abs().wrapping_mul(7) % 30_001;
            base.saturating_add(jitter as i64)
        }
        _ => match attempt {
            1 => 120_000,
            2 => 300_000,
            _ => 600_000,
        },
    }
}

fn insert_bake_run_inner(conn: &Connection, run: &NewBakeRun) -> Result<i64, StorageError> {
    conn.execute(
        "INSERT INTO bake_runs (
            trigger_reason,
            status,
            started_at,
            processed_episode_count,
            auto_created_count,
            candidate_count,
            discarded_count,
            knowledge_created_count,
            design_created_count,
            sop_created_count
         ) VALUES (?1, ?2, ?3, 0, 0, 0, 0, 0, 0, 0)",
        params![run.trigger_reason, run.status, run.started_at],
    )?;
    Ok(conn.last_insert_rowid())
}

fn row_to_bake_run(row: &rusqlite::Row<'_>) -> Result<BakeRunRecord, StorageError> {
    Ok(BakeRunRecord {
        id: row.get(0)?,
        trigger_reason: row.get(1)?,
        status: row.get(2)?,
        started_at: row.get(3)?,
        completed_at: row.get(4)?,
        processed_episode_count: row.get(5)?,
        auto_created_count: row.get(6)?,
        candidate_count: row.get(7)?,
        discarded_count: row.get(8)?,
        knowledge_created_count: row.get(9)?,
        document_created_count: row.get(10)?,
        sop_created_count: row.get(11)?,
        error_message: row.get(12)?,
        latency_ms: row.get(13)?,
    })
}

fn row_to_bake_candidate_audit(
    row: &rusqlite::Row<'_>,
) -> Result<BakeCandidateAuditRecord, rusqlite::Error> {
    Ok(BakeCandidateAuditRecord {
        id: row.get(0)?,
        run_id: row.get(1)?,
        timeline_id: row.get(2)?,
        lane: row.get(3)?,
        source_capture_count: row.get(4)?,
        effective_capture_count: row.get(5)?,
        sop_eligible: row.get::<_, i64>(6)? != 0,
        sop_eligibility_state: row.get(7)?,
        sop_eligibility_reason: row.get(8)?,
        sop_evidence_mode: row.get(9)?,
        primary_type: row.get(10)?,
        classification_reason: row.get(11)?,
        sop_model_accepted: row.get::<_, Option<i64>>(12)?.map(|value| value != 0),
        sop_model_reason: row.get(13)?,
        sop_payload_valid: row.get::<_, Option<i64>>(14)?.map(|value| value != 0),
        persist_status: row.get(15)?,
        persist_reason: row.get(16)?,
        created_at_ms: row.get(17)?,
        updated_at_ms: row.get(18)?,
    })
}

fn row_to_bake_artifact_audit(
    row: &rusqlite::Row<'_>,
) -> Result<BakeArtifactAuditRecord, rusqlite::Error> {
    Ok(BakeArtifactAuditRecord {
        id: row.get(0)?,
        run_id: row.get(1)?,
        timeline_id: row.get(2)?,
        artifact_kind: row.get(3)?,
        deterministic_eligible: row.get::<_, Option<i64>>(4)?.map(|value| value != 0),
        deterministic_reason: row.get(5)?,
        model_accepted: row.get::<_, Option<i64>>(6)?.map(|value| value != 0),
        model_reason: row.get(7)?,
        payload_present: row.get::<_, Option<i64>>(8)?.map(|value| value != 0),
        payload_valid: row.get::<_, Option<i64>>(9)?.map(|value| value != 0),
        artifact_shape: row.get(10)?,
        compatibility_recovered: row.get::<_, i64>(11)? != 0,
        persist_status: row.get(12)?,
        persist_reason: row.get(13)?,
        artifact_id: row.get(14)?,
        decision_state: row.get(15)?,
        quality_score: row.get(16)?,
        decision_reason_code: row.get(17)?,
        decision_reason_summary: row.get(18)?,
        decision_rule_version: row.get(19)?,
        shadow_payload_json: row.get(20)?,
        created_at_ms: row.get(21)?,
        updated_at_ms: row.get(22)?,
    })
}

fn row_to_bake_watermark(row: &rusqlite::Row<'_>) -> Result<BakeWatermarkRecord, StorageError> {
    Ok(BakeWatermarkRecord {
        pipeline_name: row.get(0)?,
        last_processed_ts: row.get(1)?,
        updated_at: row.get(2)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_mgr() -> StorageManager {
        StorageManager::open_in_memory().expect("内存数据库初始化失败")
    }

    #[test]
    fn candidate_audit_persists_sop_funnel_without_candidate_content() {
        let mgr = make_mgr();
        let run_id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "test".to_string(),
                status: "running".to_string(),
                started_at: 1_710_000_000_000,
            })
            .unwrap();
        mgr.upsert_bake_candidate_audit(&NewBakeCandidateAudit {
            run_id,
            timeline_id: 42,
            lane: "fresh".to_string(),
            source_capture_count: 3,
            effective_capture_count: 3,
            sop_eligible: true,
            sop_eligibility_state: "eligible".to_string(),
            sop_eligibility_reason: Some("eligible_operation_evidence".to_string()),
            sop_evidence_mode: Some("direct_interaction".to_string()),
            persist_status: "queued".to_string(),
            persist_reason: None,
        })
        .unwrap();
        mgr.update_bake_candidate_audit_model(
            run_id,
            42,
            Some("knowledge"),
            Some("事实是主资产，但同时存在操作路线"),
            true,
            None,
            Some(true),
        )
        .unwrap();
        mgr.finalize_bake_candidate_audit(run_id, 42, "created", Some("created"))
            .unwrap();

        let audit = mgr.get_bake_candidate_audit(run_id, 42).unwrap().unwrap();
        assert_eq!(audit.source_capture_count, 3);
        assert_eq!(audit.effective_capture_count, 3);
        assert!(audit.sop_eligible);
        assert_eq!(
            audit.sop_evidence_mode.as_deref(),
            Some("direct_interaction")
        );
        assert_eq!(audit.primary_type.as_deref(), Some("knowledge"));
        assert_eq!(audit.sop_model_accepted, Some(true));
        assert_eq!(audit.sop_payload_valid, Some(true));
        assert_eq!(audit.persist_status, "created");

        let funnel = mgr.get_bake_run_sop_funnel_summary(run_id).unwrap();
        assert_eq!(funnel.audited_count, 1);
        assert_eq!(funnel.eligible_count, 1);
        assert_eq!(funnel.model_accepted_count, 1);
        assert_eq!(funnel.payload_valid_count, 1);
        assert_eq!(funnel.persisted_count, 1);
    }

    #[test]
    fn operation_replay_queue_claim_and_finish_are_explicit_and_local() {
        let mgr = make_mgr();
        let run_id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "test".to_string(),
                status: "running".to_string(),
                started_at: 1_710_000_000_000,
            })
            .unwrap();
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures
                 (id, ts, event_type, is_sensitive, pii_scrubbed)
                 VALUES (42, 1710000000000, 'key_pause', 0, 0)",
                [],
            )?;
            conn.execute(
                "INSERT INTO timelines
                 (id, capture_id, summary, category, history_view, is_self_generated,
                  created_at_ms, updated_at_ms)
                 VALUES (42, 42, 'test', 'coding', 0, 0, 1710000000000, 1710000000000)",
                [],
            )?;
            conn.execute(
                "INSERT INTO operation_replay_queue
                 (timeline_id, reason, status, priority, queued_at_ms)
                 VALUES (42, 'test', 'pending', 10, 1710000000000)",
                [],
            )?;
            Ok(())
        })
        .unwrap();

        assert_eq!(mgr.count_pending_operation_replays(3).unwrap(), 1);
        assert!(mgr.claim_operation_replay(42, run_id).unwrap());
        assert_eq!(mgr.count_pending_operation_replays(3).unwrap(), 0);
        mgr.finish_operation_replay(42, "pending").unwrap();
        assert_eq!(mgr.count_pending_operation_replays(3).unwrap(), 1);
        assert!(mgr.claim_operation_replay(42, run_id).unwrap());
        mgr.finish_operation_replay(42, "completed").unwrap();
        assert_eq!(mgr.count_pending_operation_replays(3).unwrap(), 0);
    }

    #[test]
    fn artifact_audits_preserve_independent_branch_decisions() {
        let mgr = make_mgr();
        let run_id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "test".to_string(),
                status: "running".to_string(),
                started_at: 1_710_000_000_000,
            })
            .unwrap();

        for (kind, accepted, reason) in [
            ("knowledge", true, None),
            ("document", false, Some("not_a_document")),
            ("sop", false, Some("missing_real_action")),
        ] {
            mgr.upsert_bake_artifact_audit(&NewBakeArtifactAudit {
                run_id,
                timeline_id: 42,
                artifact_kind: kind.to_string(),
                deterministic_eligible: (kind != "knowledge").then_some(kind == "document"),
                deterministic_reason: None,
                model_accepted: accepted,
                model_reason: reason.map(ToString::to_string),
                payload_present: accepted,
                payload_valid: accepted.then_some(true),
                artifact_shape: Some("object".to_string()),
                compatibility_recovered: kind == "document",
            })
            .unwrap();
        }
        mgr.finalize_bake_artifact_audit(
            run_id,
            42,
            "knowledge",
            "created",
            Some("created"),
            Some(2535),
        )
        .unwrap();
        mgr.finalize_bake_artifact_audit(
            run_id,
            42,
            "document",
            "false_negative",
            Some("not_a_document"),
            None,
        )
        .unwrap();
        mgr.finalize_bake_artifact_audit(
            run_id,
            42,
            "sop",
            "rejected",
            Some("missing_real_action"),
            None,
        )
        .unwrap();

        let audits = mgr.list_bake_artifact_audits_for_timeline(42, 10).unwrap();
        assert_eq!(audits.len(), 3);
        let knowledge = audits
            .iter()
            .find(|audit| audit.artifact_kind == "knowledge")
            .unwrap();
        assert_eq!(knowledge.persist_status, "created");
        assert_eq!(knowledge.artifact_id, Some(2535));
        let document = audits
            .iter()
            .find(|audit| audit.artifact_kind == "document")
            .unwrap();
        assert_eq!(document.model_accepted, Some(false));
        assert_eq!(document.persist_status, "false_negative");
        assert!(document.compatibility_recovered);
        let sop = audits
            .iter()
            .find(|audit| audit.artifact_kind == "sop")
            .unwrap();
        assert_eq!(sop.persist_status, "rejected");
        assert_eq!(sop.persist_reason.as_deref(), Some("missing_real_action"));
    }

    #[test]
    fn production_events_count_completed_merges_without_using_asset_updates() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE bake_knowledge (
                id INTEGER PRIMARY KEY,
                content TEXT,
                created_at TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER
            );
            CREATE TABLE bake_sops (
                id INTEGER PRIMARY KEY,
                content TEXT,
                created_at TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER
            );
            CREATE TABLE bake_documents (
                id INTEGER PRIMARY KEY,
                creation_mode TEXT,
                generation_version TEXT,
                created_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE bake_runs (
                id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                completed_at INTEGER,
                knowledge_created_count INTEGER NOT NULL DEFAULT 0,
                design_created_count INTEGER NOT NULL DEFAULT 0,
                sop_created_count INTEGER NOT NULL DEFAULT 0
            );

            INSERT INTO bake_knowledge VALUES
                (1, '{"creation_mode":"manual"}', NULL, 3000, 3000),
                (2, '{"creation_mode":"llm_bake"}', NULL, 3100, 3600),
                (3, '{"creation_mode":"auto","generation_version":"bake-v1"}', NULL, 3200, 3200),
                (4, '{"creation_mode":"manual"}', NULL, 1000, 3700);
            INSERT INTO bake_documents VALUES
                (1, 'manual', NULL, 3300, 3300),
                (2, 'llm_bake', 'bake-v1', 1000, 3500);
            INSERT INTO bake_sops VALUES
                (1, '{"creation_mode":"manual"}', NULL, 3400, 3400),
                (2, '{"creation_mode":"llm_bake"}', NULL, 3450, 3450);
            INSERT INTO bake_runs VALUES
                (1, 'completed', 3500, 2, 1, 3),
                (2, 'failed', 3600, 9, 9, 9);
            "#,
        )
        .unwrap();

        let events = load_bake_production_events(&conn, 2500).unwrap();
        let totals =
            events
                .iter()
                .fold(BakeProductionEventRecord::default(), |mut total, event| {
                    total.knowledge_count += event.knowledge_count;
                    total.document_count += event.document_count;
                    total.sop_count += event.sop_count;
                    total
                });

        assert_eq!(totals.knowledge_count, 3);
        assert_eq!(totals.document_count, 2);
        assert_eq!(totals.sop_count, 4);
        assert!(events
            .iter()
            .any(|event| { event.occurred_at_ms == 3500 && event.document_count == 1 }));
    }

    #[test]
    fn test_insert_and_complete_bake_run() {
        let mgr = make_mgr();
        let id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "manual_debug".to_string(),
                status: "running".to_string(),
                started_at: 123,
            })
            .unwrap();

        assert!(mgr
            .complete_bake_run(id, "completed", 456, 3, 1, 1, 1, 1, 0, 0, None, Some(333),)
            .unwrap());

        let latest = mgr.get_latest_bake_run().unwrap().unwrap();
        assert_eq!(latest.id, id);
        assert_eq!(latest.status, "completed");
        assert_eq!(latest.processed_episode_count, 3);
        assert_eq!(latest.latency_ms, Some(333));
    }

    #[test]
    fn test_defer_bake_run_preserves_progress() {
        let mgr = make_mgr();
        let id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "automatic".to_string(),
                status: "running".to_string(),
                started_at: 100,
            })
            .unwrap();
        mgr.update_bake_run_progress(id, 20, 4).unwrap();

        assert!(mgr
            .defer_bake_run_preserving_progress(id, 250, "interactive preemption", Some(150))
            .unwrap());

        let latest = mgr.get_latest_bake_run().unwrap().unwrap();
        assert_eq!(latest.status, "deferred");
        assert_eq!(latest.candidate_count, 20);
        assert_eq!(latest.processed_episode_count, 4);
        assert_eq!(
            latest.error_message.as_deref(),
            Some("interactive preemption")
        );
    }

    #[test]
    fn test_clear_retry_failure_and_recoverable_history() {
        let mgr = make_mgr();
        mgr.with_conn(|conn| {
            for id in [101_i64, 102, 103, 104, 105, 106, 107, 108] {
                conn.execute(
                    "INSERT INTO captures (id, ts, event_type) VALUES (?1, ?1, 'manual')",
                    params![id],
                )?;
                conn.execute(
                    "INSERT INTO timelines (id, capture_id, summary) VALUES (?1, ?1, 'test')",
                    params![id],
                )?;
                conn.execute(
                    "UPDATE timelines SET updated_at_ms = ?1 WHERE id = ?1",
                    params![id],
                )?;
            }
            Ok(())
        })
        .unwrap();
        mgr.upsert_bake_watermark("unified", 1_000).unwrap();
        mgr.bump_bake_retry_failure(101, "upstream error (503 Service Unavailable): busy")
            .unwrap();
        mgr.bump_bake_retry_failure(
            102,
            "internal error: 解析 merge_document 响应失败: missing field `title`",
        )
        .unwrap();
        mgr.bump_bake_retry_failure(103, "internal error: invalid permanent payload")
            .unwrap();
        mgr.bump_bake_retry_failure(
            104,
            "internal error: 解析 bake sop payload 失败: invalid type: map, expected a string",
        )
        .unwrap();
        mgr.bump_bake_retry_failure(105, "upstream error (504 Gateway Timeout): bake 提炼超时")
            .unwrap();
        mgr.bump_bake_retry_failure(
            106,
            "internal error: 解析 bake knowledge payload 失败: missing field `summary`",
        )
        .unwrap();
        mgr.bump_bake_retry_failure(107, "upstream error (429 Too Many Requests): busy")
            .unwrap();
        mgr.bump_bake_retry_failure(
            108,
            "bake_error code=BAKE_UNCLASSIFIED_UPSTREAM_ERROR status=502",
        )
        .unwrap();

        assert_eq!(mgr.get_bake_retry_failure_count(105).unwrap(), 1);
        assert_eq!(mgr.get_bake_retry_failure_count(999).unwrap(), 0);
        assert_eq!(mgr.clear_recoverable_bake_retry_failures().unwrap(), 5);
        assert_eq!(
            mgr.get_bake_watermark("unified")
                .unwrap()
                .unwrap()
                .last_processed_ts,
            100
        );
        assert!(!mgr.clear_bake_retry_failure(101).unwrap());
        assert!(!mgr.clear_bake_retry_failure(104).unwrap());
        assert!(!mgr.clear_bake_retry_failure(106).unwrap());
        assert!(!mgr.clear_bake_retry_failure(107).unwrap());
        assert_eq!(mgr.get_bake_retry_failure_count(108).unwrap(), 1);
        assert!(mgr.clear_bake_retry_failure(105).unwrap());
        assert!(mgr.clear_bake_retry_failure(103).unwrap());
        assert!(!mgr.clear_bake_retry_failure(103).unwrap());
    }

    #[test]
    fn test_retry_schedule_uses_error_specific_persistent_backoff() {
        let mgr = make_mgr();
        mgr.with_conn(|conn| {
            for id in [201_i64, 202, 203] {
                conn.execute(
                    "INSERT INTO captures (id, ts, event_type) VALUES (?1, ?1, 'manual')",
                    params![id],
                )?;
                conn.execute(
                    "INSERT INTO timelines (id, capture_id, summary) VALUES (?1, ?1, 'test')",
                    params![id],
                )?;
            }
            Ok(())
        })
        .unwrap();

        mgr.bump_bake_retry_failure_with_code(201, "invalid output", "BAKE_OUTPUT_INVALID")
            .unwrap();
        mgr.bump_bake_retry_failure_with_code(202, "timeout", "INFERENCE_TIMEOUT")
            .unwrap();
        mgr.bump_bake_retry_failure_with_code(
            203,
            "upstream 502",
            "BAKE_UNCLASSIFIED_UPSTREAM_ERROR",
        )
        .unwrap();

        let output = mgr.get_bake_retry_state(201).unwrap().unwrap();
        let timeout = mgr.get_bake_retry_state(202).unwrap().unwrap();
        let upstream = mgr.get_bake_retry_state(203).unwrap().unwrap();
        assert_eq!(
            output.last_error_code.as_deref(),
            Some("BAKE_OUTPUT_INVALID")
        );
        assert!(output.next_retry_at_ms - output.last_failed_at_ms >= 10_000);
        assert!(timeout.next_retry_at_ms - timeout.last_failed_at_ms >= 60_000);
        assert!(upstream.next_retry_at_ms - upstream.last_failed_at_ms >= 300_000);

        mgr.bump_bake_retry_failure_with_code(
            203,
            "upstream 502",
            "BAKE_UNCLASSIFIED_UPSTREAM_ERROR",
        )
        .unwrap();
        let upstream_second = mgr.get_bake_retry_state(203).unwrap().unwrap();
        assert_eq!(upstream_second.failure_count, 2);
        assert!(upstream_second.next_retry_at_ms - upstream_second.last_failed_at_ms >= 600_000);
    }

    #[test]
    fn test_queue_status_does_not_count_completed_watermark_progress_as_no_progress() {
        let mgr = make_mgr();
        for started_at in [100_i64, 200, 300] {
            let run_id = mgr
                .insert_bake_run(&NewBakeRun {
                    trigger_reason: "knowledge_background".to_string(),
                    status: "running".to_string(),
                    started_at,
                })
                .unwrap();
            mgr.complete_bake_run(
                run_id,
                "completed",
                started_at + 10,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                Some(10),
            )
            .unwrap();
        }

        let queue = mgr.get_bake_queue_status(3).unwrap();
        assert_eq!(queue.recent_no_progress_count, 0);
        assert_eq!(queue.actionable_count, 0);
        assert_eq!(queue.recommended_retry_after_ms, 300_000);
    }

    #[test]
    fn test_queue_status_counts_only_consecutive_explicit_no_op_runs() {
        let mgr = make_mgr();
        let now = current_ts_ms();
        for (offset, status, processed) in [
            (0_i64, "no_op", 0_i64),
            (1, "completed", 1),
            (2, "no_op", 0),
            (3, "no_op", 0),
        ] {
            let started_at = now - 4_000 + offset;
            let run_id = mgr
                .insert_bake_run(&NewBakeRun {
                    trigger_reason: "knowledge_background".to_string(),
                    status: "running".to_string(),
                    started_at,
                })
                .unwrap();
            mgr.complete_bake_run(
                run_id,
                status,
                now - 1_000 + offset,
                processed,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                Some(10),
            )
            .unwrap();
        }

        let queue = mgr.get_bake_queue_status(3).unwrap();
        assert_eq!(queue.recent_no_progress_count, 2);
    }

    /// 回归：队列排除文档已覆盖 timeline 的 CTE 口径必须与旧逐条 json_each
    /// 版本一致，包括 source_memory_ids / source_episode_ids 两条路径，以及
    /// 软删文档不参与排除。
    #[test]
    fn test_queue_status_excludes_timelines_referenced_by_documents() {
        let mgr = make_mgr();
        mgr.with_conn(|conn| {
            for id in [211_i64, 212, 213] {
                conn.execute(
                    "INSERT INTO captures (id, ts, event_type) VALUES (?1, ?1, 'manual')",
                    params![id],
                )?;
                conn.execute(
                    "INSERT INTO timelines (id, capture_id, summary) VALUES (?1, ?1, 'test')",
                    params![id],
                )?;
                conn.execute(
                    "UPDATE timelines SET updated_at_ms = ?2 WHERE id = ?1",
                    params![id, id + 1_000_000],
                )?;
            }
            // category 为 NULL 时 NOT IN 谓词会直接排除，需给出合法分类与高价值标记。
            conn.execute(
                "UPDATE timelines SET category = 'work', importance = 5 WHERE id IN (211, 212, 213)",
                [],
            )?;
            // 211 被 source_memory_ids 覆盖，212 被 source_episode_ids 覆盖；
            // 引用 213 的文档已软删，不应参与排除。
            conn.execute_batch(
                "INSERT INTO bake_documents (id, title, source_memory_ids, created_at, updated_at)
                     VALUES (1, 'd1', '[\"211\"]', 1, 1);
                 INSERT INTO bake_documents (id, title, source_episode_ids, created_at, updated_at)
                     VALUES (2, 'd2', '[\"212\"]', 1, 1);
                 INSERT INTO bake_documents (id, title, source_memory_ids, deleted_at, created_at, updated_at)
                     VALUES (3, 'd3', '[\"213\"]', 123, 1, 1);",
            )?;
            Ok(())
        })
        .unwrap();
        mgr.upsert_bake_watermark("unified", 0).unwrap();

        let queue = mgr.get_bake_queue_status(3).unwrap();
        assert_eq!(queue.fresh_count, 1, "只有未被文档覆盖的 213 应留在队列");
        assert_eq!(queue.pending_count, 1);
    }

    /// 回归：metadata-refresh 口径的 CTE 改写必须保持原语义：文档引用了
    /// timeline 但尚未收录其全部成员 capture 时才计入；补齐后应清零。
    #[test]
    fn test_queue_status_metadata_refresh_counts_missing_capture_members() {
        let mgr = make_mgr();
        mgr.with_conn(|conn| {
            conn.execute(
                "INSERT INTO captures (id, ts, event_type) VALUES (311, 311, 'manual')",
                [],
            )?;
            conn.execute(
                "INSERT INTO captures (id, ts, event_type, timeline_id)
                 VALUES (312, 312, 'manual', 301)",
                [],
            )?;
            conn.execute(
                "INSERT INTO timelines (id, capture_id, summary) VALUES (301, 311, 'test')",
                [],
            )?;
            conn.execute("UPDATE timelines SET updated_at_ms = 5 WHERE id = 301", [])?;
            // 文档引用 timeline 301 但只收录了 capture 311，312 缺失。
            conn.execute(
                "INSERT INTO bake_documents
                     (id, title, source_memory_ids, source_capture_ids, created_at, updated_at)
                 VALUES (1, 'doc-301', '[\"301\"]', '[311]', 1, 1)",
                [],
            )?;
            Ok(())
        })
        .unwrap();
        // 水位线高于 candidate_ts，确保 301 不会同时进 fresh lane。
        mgr.upsert_bake_watermark("unified", 1_000).unwrap();

        let queue = mgr.get_bake_queue_status(3).unwrap();
        assert_eq!(queue.metadata_refresh_count, 1);
        assert_eq!(queue.fresh_count, 0);
        assert_eq!(queue.actionable_count, 1);

        // 补齐成员 capture 后不再需要 metadata 刷新。
        mgr.with_conn(|conn| {
            conn.execute(
                "UPDATE bake_documents SET source_capture_ids = '[311, 312]' WHERE id = 1",
                [],
            )?;
            Ok(())
        })
        .unwrap();
        let queue = mgr.get_bake_queue_status(3).unwrap();
        assert_eq!(queue.metadata_refresh_count, 0);
        assert_eq!(queue.actionable_count, 0);
    }

    #[test]
    fn test_fail_bake_run_preserves_recorded_progress() {
        let mgr = make_mgr();
        let id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "preempted".to_string(),
                status: "running".to_string(),
                started_at: 100,
            })
            .unwrap();
        mgr.update_bake_run_progress(id, 7, 2).unwrap();
        mgr.fail_bake_run_preserving_progress(id, 300, "retry later", Some(200))
            .unwrap();

        let run = mgr.get_latest_bake_run().unwrap().unwrap();
        assert_eq!(run.status, "failed");
        assert_eq!(run.candidate_count, 7);
        assert_eq!(run.processed_episode_count, 2);
        assert_eq!(run.error_message.as_deref(), Some("retry later"));
        assert_eq!(run.latency_ms, Some(200));
    }

    #[test]
    fn test_upsert_and_get_bake_watermark() {
        let mgr = make_mgr();
        mgr.upsert_bake_watermark("unified", 100).unwrap();
        mgr.upsert_bake_watermark("unified", 200).unwrap();
        let watermark = mgr.get_bake_watermark("unified").unwrap().unwrap();
        assert_eq!(watermark.pipeline_name, "unified");
        assert_eq!(watermark.last_processed_ts, 200);
    }

    #[test]
    fn test_fail_stale_running_bake_runs_preserves_fresh_run() {
        let mgr = make_mgr();
        let now = current_ts_ms();
        let stale_id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "stale".to_string(),
                status: "running".to_string(),
                started_at: now - STALE_RUNNING_BAKE_RUN_MS - 1,
            })
            .unwrap();
        let fresh_id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "fresh".to_string(),
                status: "running".to_string(),
                started_at: now,
            })
            .unwrap();

        assert_eq!(mgr.fail_stale_running_bake_runs().unwrap(), 1);
        let (stale_status, stale_completed, fresh_status): (String, Option<i64>, String) = mgr
            .with_conn(|conn| {
                let (stale_status, stale_completed) = conn.query_row(
                    "SELECT status, completed_at FROM bake_runs WHERE id = ?1",
                    params![stale_id],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )?;
                let fresh_status = conn.query_row(
                    "SELECT status FROM bake_runs WHERE id = ?1",
                    params![fresh_id],
                    |row| row.get(0),
                )?;
                Ok((stale_status, stale_completed, fresh_status))
            })
            .unwrap();
        assert_eq!(stale_status, "failed");
        assert!(stale_completed.is_some());
        assert_eq!(fresh_status, "running");
    }

    #[test]
    fn test_startup_recovery_fails_even_fresh_orphaned_run() {
        let mgr = make_mgr();
        let now = current_ts_ms();
        let id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "orphaned_by_restart".to_string(),
                status: "running".to_string(),
                started_at: now,
            })
            .unwrap();
        mgr.update_bake_run_progress(id, 15, 3).unwrap();

        assert_eq!(mgr.fail_orphaned_running_bake_runs_on_startup().unwrap(), 1);
        assert_eq!(mgr.count_running_bake_runs().unwrap(), 0);

        let run = mgr.get_latest_bake_run().unwrap().unwrap();
        assert_eq!(run.status, "failed");
        assert_eq!(run.candidate_count, 15);
        assert_eq!(run.processed_episode_count, 3);
        assert_eq!(
            run.error_message.as_deref(),
            Some("orphaned running bake run recovered on startup")
        );
    }

    #[test]
    fn test_stale_terminal_run_rejects_late_progress_and_completion() {
        let mgr = make_mgr();
        let now = current_ts_ms();
        let stale_id = mgr
            .insert_bake_run(&NewBakeRun {
                trigger_reason: "orphaned_by_restart".to_string(),
                status: "running".to_string(),
                started_at: now - STALE_RUNNING_BAKE_RUN_MS - 1,
            })
            .unwrap();

        assert_eq!(mgr.fail_stale_running_bake_runs().unwrap(), 1);
        assert!(!mgr.update_bake_run_progress(stale_id, 10, 3).unwrap());
        assert!(!mgr
            .complete_bake_run(
                stale_id,
                "completed",
                now,
                3,
                1,
                1,
                0,
                1,
                0,
                0,
                None,
                Some(STALE_RUNNING_BAKE_RUN_MS),
            )
            .unwrap());

        let run = mgr.get_latest_bake_run().unwrap().unwrap();
        assert_eq!(run.status, "failed");
        assert_eq!(run.candidate_count, 0);
        assert_eq!(run.processed_episode_count, 0);
    }
}
