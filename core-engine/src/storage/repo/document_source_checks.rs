use crate::storage::{error::StorageError,StorageManager};
use rusqlite::{params,OptionalExtension};
use serde_json::{json,Value};
use sha2::{Digest,Sha256};

impl StorageManager {
    /// Store failure status and its audit together; never leave conflicting views.
    pub fn record_document_refresh_failure_check(&self, document_id:i64, checked_at:i64,
        error_code:&str, evidence:&Value) -> Result<bool,StorageError> {
        self.with_conn(|conn| {
            let tx=conn.unchecked_transaction()?;
            let changed=tx.execute("UPDATE bake_documents SET last_refresh_checked_at_ms=?2,
                last_refresh_error=?3,last_refresh_status=CASE WHEN last_refresh_success_at_ms>0
                    THEN 'historical_only' ELSE 'unavailable' END
                WHERE id=?1 AND deleted_at IS NULL",params![document_id,checked_at,error_code])?;
            if changed==0 { return Ok(false); }
            tx.execute("INSERT INTO bake_document_source_checks(document_id,snapshot_id,checked_at,evidence_json)
                VALUES(?1,NULL,?2,?3)",params![document_id,checked_at,evidence.to_string()])?;
            tx.commit()?;
            Ok(true)
        })
    }

    /// Local aggregate diagnostics. Never return page text, URLs or arbitrary errors.
    pub fn document_source_health(&self, since_ms:i64, now_ms:i64) -> Result<Value,StorageError> {
        self.with_conn(|conn| {
            let checks = conn.query_row(
                "WITH samples AS (
                    SELECT CASE WHEN json_valid(evidence_json) THEN evidence_json ELSE '{}' END AS e
                    FROM bake_document_source_checks WHERE checked_at>=?1 AND checked_at<=?2
                 ), classified AS (
                    SELECT json_extract(e,'$.coverage') AS coverage,
                           json_extract(e,'$.reason') AS reason,
                           CASE WHEN json_type(e,'$.execution_ms') IN ('integer','real')
                             AND json_extract(e,'$.execution_ms')>=0
                             THEN json_extract(e,'$.execution_ms') END AS elapsed
                    FROM samples)
                 SELECT COUNT(*),COALESCE(SUM(coverage='complete'),0),COALESCE(SUM(coverage='partial'),0),
                    COALESCE(SUM(coverage='failed'),0),
                    COALESCE(SUM(reason IN ('SCRAPE_TIMEOUT','BROWSER_EXTENSION_TIMEOUT','RESOURCE_BUDGET_EXCEEDED')),0),
                    COUNT(elapsed),AVG(elapsed),MAX(elapsed) FROM classified",
                params![since_ms,now_ms], |r| Ok(json!({
                    "total":r.get::<_,i64>(0)?,"complete":r.get::<_,i64>(1)?,
                    "partial":r.get::<_,i64>(2)?,"failed":r.get::<_,i64>(3)?,
                    "budget_or_timeout":r.get::<_,i64>(4)?,"timed_samples":r.get::<_,i64>(5)?,
                    "mean_execution_ms":r.get::<_,Option<f64>>(6)?,"max_execution_ms":r.get::<_,Option<f64>>(7)?
                })))?;
            let mut observations=conn.query_row(
                "SELECT COUNT(*),COALESCE(SUM(state='pending'),0),COALESCE(SUM(state='running'),0),
                 COALESCE(SUM(state='blocked'),0),COALESCE(SUM(state='completed'),0),
                 COALESCE(SUM(attempts>1),0),
                 MAX(CASE WHEN state='pending' THEN MAX(0,?2-created_at) END),COALESCE(SUM(duplicate_count),0)
                 FROM bake_document_refresh_observations WHERE created_at>=?1 AND created_at<=?2",
                params![since_ms,now_ms], |r| Ok(json!({
                    "total":r.get::<_,i64>(0)?,"pending":r.get::<_,i64>(1)?,
                    "running":r.get::<_,i64>(2)?,"blocked":r.get::<_,i64>(3)?,
                    "completed":r.get::<_,i64>(4)?,"retried_observations":r.get::<_,i64>(5)?,
                    "oldest_pending_age_ms":r.get::<_,Option<i64>>(6)?,
                    "duplicate_enqueue_count":r.get::<_,i64>(7)?
                })))?;
            let mut duplicate_stmt=conn.prepare("SELECT document_id,SUM(duplicate_count)
                FROM bake_document_refresh_observations WHERE created_at>=?1 AND created_at<=?2
                GROUP BY document_id HAVING SUM(duplicate_count)>0 ORDER BY document_id")?;
            observations["duplicate_enqueues_by_document"]=json!(duplicate_stmt.query_map(
                params![since_ms,now_ms],|r|Ok(json!({"document_id":r.get::<_,i64>(0)?,
                    "duplicate_enqueue_count":r.get::<_,i64>(1)?})))?
                .collect::<Result<Vec<_>,_>>()?);
            let attempts=conn.query_row(
                "SELECT COUNT(*),COALESCE(SUM(outcome='running'),0),COALESCE(SUM(outcome='completed'),0),
                 COALESCE(SUM(outcome='pending'),0),COALESCE(SUM(outcome='blocked'),0),
                 COALESCE(SUM(outcome='cancelled'),0),COALESCE(SUM(outcome='interrupted'),0),COALESCE(SUM(outcome='paused' AND execution_wall_ms IS NULL),0),
                 AVG(queue_wait_ms),COUNT(execution_wall_ms),AVG(execution_wall_ms),COALESCE(SUM(scheduled_retry_ms),0),
                 COALESCE(SUM(outcome='paused' AND execution_wall_ms IS NOT NULL),0)
                 FROM bake_document_refresh_attempt_metrics WHERE started_at>=?1 AND started_at<=?2",
                params![since_ms,now_ms],|r|Ok(json!({
                    "claimed":r.get::<_,i64>(0)?,"running":r.get::<_,i64>(1)?,"completed":r.get::<_,i64>(2)?,
                    "retry_scheduled":r.get::<_,i64>(3)?,"blocked":r.get::<_,i64>(4)?,"cancelled":r.get::<_,i64>(5)?,
                    "interrupted":r.get::<_,i64>(6)?,"paused_before_dispatch":r.get::<_,i64>(7)?,
                    "mean_queue_wait_ms":r.get::<_,Option<f64>>(8)?,"finished_timing_samples":r.get::<_,i64>(9)?,
                    "mean_execution_wall_ms":r.get::<_,Option<f64>>(10)?,"scheduled_retry_ms":r.get::<_,i64>(11)?,
                    "paused_after_dispatch":r.get::<_,i64>(12)?
                })))?;
            let mut mismatch_stmt=conn.prepare("SELECT component,reason,SUM(occurrences)
                FROM document_source_mismatch_events WHERE observed_at>=?1 AND observed_at<=?2
                GROUP BY component,reason ORDER BY component,reason")?;
            let mismatches=mismatch_stmt.query_map(params![since_ms,now_ms],|r| Ok(json!({
                "component":r.get::<_,String>(0)?,"reason":r.get::<_,String>(1)?,
                "occurrences":r.get::<_,i64>(2)?
            })))?.collect::<Result<Vec<_>,_>>()?;
            let mut candidate_stmt=conn.prepare("SELECT stage,json_extract(evidence_json,'$.reason'),COUNT(*)
                FROM document_candidate_quality_events WHERE observed_at>=?1 AND observed_at<=?2
                GROUP BY stage,json_extract(evidence_json,'$.reason') ORDER BY stage,2")?;
            let candidate_evaluations=candidate_stmt.query_map(params![since_ms,now_ms],|r| Ok(json!({
                "stage":r.get::<_,String>(0)?,"reason":r.get::<_,String>(1)?,
                "evaluations":r.get::<_,i64>(2)?
            })))?.collect::<Result<Vec<_>,_>>()?;
            let mut coalesce_stmt=conn.prepare("SELECT document_source_identity_hash,COUNT(*)
                FROM bake_candidate_audits WHERE updated_at_ms>=?1 AND updated_at_ms<=?2
                    AND persist_status='skipped' AND persist_reason='document_url_already_queued'
                GROUP BY document_source_identity_hash ORDER BY document_source_identity_hash")?;
            let coalesced_sources=coalesce_stmt.query_map(params![since_ms,now_ms],|r|Ok(json!({
                "source_identity_hash":r.get::<_,Option<String>>(0)?,"skipped_candidates":r.get::<_,i64>(1)?
            })))?.collect::<Result<Vec<_>,_>>()?;
            let coalesced_total:i64=coalesced_sources.iter().filter_map(|v|v["skipped_candidates"].as_i64()).sum();
            Ok(json!({"schema_version":"document-source-health.v1", "since_ms":since_ms,
                "as_of_ms":now_ms,"checks":checks,"observations":observations,"attempts":attempts,
                "version_mismatches":mismatches,"candidate_evaluations":candidate_evaluations,
                "prequeue_coalescing":{"total":coalesced_total,"sources":coalesced_sources}}))
        })
    }
    pub fn record_document_source_check(&self, document_id:i64, snapshot_id:Option<i64>, checked_at:i64,
        evidence:&Value) -> Result<i64,StorageError> {
        self.with_conn(|conn| {
            let changed=conn.execute("INSERT INTO bake_document_source_checks(document_id,snapshot_id,checked_at,evidence_json)
                SELECT ?1,?2,?3,?4 WHERE ?2 IS NULL OR EXISTS(
                    SELECT 1 FROM bake_document_source_snapshots WHERE id=?2 AND document_id=?1)",
                params![document_id,snapshot_id,checked_at,evidence.to_string()])?;
            if changed==0 { return Err(rusqlite::Error::InvalidQuery.into()); }
            Ok(conn.last_insert_rowid())
        })
    }
    pub fn latest_document_source_check(&self, document_id:i64) -> Result<Option<Value>,StorageError> {
        self.with_conn(|conn| Ok(conn.query_row("SELECT id,snapshot_id,checked_at,evidence_json
            FROM bake_document_source_checks WHERE document_id=?1 ORDER BY id DESC LIMIT 1",params![document_id],|r| {
                let evidence:String=r.get(3)?;
                Ok(json!({"id":r.get::<_,i64>(0)?,"snapshot_id":r.get::<_,Option<i64>>(1)?,
                    "checked_at_ms":r.get::<_,i64>(2)?,"evidence":serde_json::from_str::<Value>(&evidence).unwrap_or(Value::Null)}))
            }).optional()?))
    }
}

fn normalized_with_offsets(text:&str) -> (String,Vec<(usize,usize)>) {
    let mut normalized=String::new();
    let mut offsets=Vec::new();
    for (start,c) in text.char_indices() {
        if c.is_whitespace() || matches!(c,'\u{200b}'|'\u{feff}') { continue; }
        normalized.push(c);
        for _ in 0..c.len_utf8() { offsets.push((start,start+c.len_utf8())); }
    }
    (normalized,offsets)
}

/// Store ranges into the already-redacted snapshot, never raw block text or arbitrary page metadata.
pub fn build_document_source_evidence(structured:&Value, snapshot_text:&str, coverage:&str, redactions:u64) -> Value {
    let collector=structured.pointer("/document_body/version").and_then(Value::as_str)
        .filter(|v|matches!(*v,"document-body.v2"|"document-body.v3")).unwrap_or("browser_attach");
    let (normalized,offsets)=normalized_with_offsets(snapshot_text);
    let supplied=structured.pointer("/document_body/blocks").and_then(Value::as_array);
    let mut blocks=Vec::new();
    let mut cursor=0;
    let mut unmatched=0;
    let mut substantive_candidates=0;
    if let Some(supplied)=supplied {
        for (ordinal,block) in supplied.iter().take(5000).enumerate() {
            let needle=normalized_with_offsets(block.get("text").and_then(Value::as_str).unwrap_or("")).0;
            let Some(relative)=(!needle.is_empty()).then(||normalized[cursor..].find(&needle)).flatten() else { unmatched+=1;continue; };
            let start=cursor+relative;
            let end=start+needle.len();
            let start_byte=offsets[start].0;
            let end_byte=offsets[end-1].1;
            let candidate=!crate::services::bake_service::is_document_shell(&snapshot_text[start_byte..end_byte]);
            if candidate {substantive_candidates+=1;}
            let kind=block.get("type").and_then(Value::as_str)
                .filter(|v|matches!(*v,"paragraph"|"heading"|"list"|"code"|"table"|"table_cell"|"text"))
                .unwrap_or("text");
            let mut reference=json!({"block_id":format!("b{ordinal}"),"ordinal":ordinal,"type":kind,
                "body_candidate":candidate,
                "start_byte":start_byte,"end_byte":end_byte,
                "text_sha256":format!("{:x}",Sha256::digest(snapshot_text[start_byte..end_byte].as_bytes()))});
            for field in ["page","top","left","row","column","columns","row_span","column_span","level"] {
                if let Some(_value)=block.get(field).and_then(Value::as_f64).filter(|v|v.is_finite() && v.abs()<=10_000_000.0) {
                    reference[field]=block[field].clone();
                }
            }
            if let Some(path)=block.get("dom_path").and_then(Value::as_array)
                .filter(|path|path.len()<=64 && path.iter().all(|v|v.as_u64().is_some_and(|n|n<=1_000_000))) {
                reference["dom_path"]=json!(path);
            }
            blocks.push(reference);
            cursor=end;
        }
    }
    let adapter=structured.pointer("/document_body/adapter").and_then(Value::as_str)
        .filter(|v|matches!(*v,"paginated_editor"|"semantic_document"|"article"|"main")).unwrap_or("unknown");
    let quality=structured.pointer("/document_body/quality").and_then(Value::as_str)
        .filter(|v|matches!(*v,"substantive"|"shell"|"unknown")).unwrap_or("unknown");
    let mut evidence=json!({"quality_version":crate::services::document_refresh::DOCUMENT_QUALITY_RULE_VERSION,"extractor_version":collector,
        "adapter":adapter,"body_quality":quality,"offset_unit":"utf8_bytes",
        "body_character_count":snapshot_text.chars().count(),
        "substantive_block_count":if supplied.is_some_and(|v|v.len()<=5000) && unmatched==0 {Some(substantive_candidates)} else {None},
        "substantive_block_rule":"aligned-non-shell.v1",
        "coverage":coverage,"redaction_count":redactions,
        "snapshot_text_sha256":format!("{:x}",Sha256::digest(snapshot_text.as_bytes())),
        "block_reference_count":blocks.len(),"unmatched_blocks":unmatched,
        "block_limit_exceeded":supplied.is_some_and(|v|v.len()>5000),"blocks":blocks});
    for field in ["matching_passes","final_stable_passes","block_count","excluded_block_count"] {
        if let Some(number)=structured.pointer(&format!("/document_body/{field}")).and_then(Value::as_u64) {
            evidence[field]=json!(number);
        }
    }
    if structured.pointer("/document_body/exclusion_scope").and_then(Value::as_str)==Some("final_body_root") {
        evidence["exclusion_scope"]=json!("final_body_root");
    }
    for field in ["final_snapshot_covers_observed","virtualized_or_changed"] {
        if let Some(value)=structured.pointer(&format!("/document_body/{field}")).and_then(Value::as_bool) {
            evidence[field]=json!(value);
        }
    }
    let mut completeness=json!({});
    for field in ["reached_end", "truncated"] {
        if let Some(value)=structured.pointer(&format!("/completeness/{field}")).and_then(Value::as_bool) {
            completeness[field]=json!(value);
        }
    }
    for field in ["stable_passes", "segment_count", "character_count"] {
        if let Some(value)=structured.pointer(&format!("/completeness/{field}")).and_then(Value::as_u64) {
            completeness[field]=json!(value);
        }
    }
    evidence["completeness_evidence"]=completeness;
    evidence
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn document_refresh_failure_and_audit_commit_or_rollback_together() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| {
            c.execute("INSERT INTO bake_documents(id,title,doc_type,full_content,created_at,updated_at,last_refresh_success_at_ms,last_refresh_status)
                VALUES(1,'test','document','preserved body',1,1,10,'fresh_complete')",[])?;
            Ok(())
        }).unwrap();
        let evidence=json!({"coverage":"failed","reason":"SOURCE_WRITES_PAUSED"});
        assert!(db.record_document_refresh_failure_check(1,20,"SOURCE_WRITES_PAUSED",&evidence).unwrap());
        db.with_conn(|c| {
            assert_eq!(c.query_row("SELECT last_refresh_status FROM bake_documents WHERE id=1",[],|r|r.get::<_,String>(0))?,"historical_only");
            c.execute_batch("CREATE TRIGGER reject_check BEFORE INSERT ON bake_document_source_checks BEGIN SELECT RAISE(ABORT,'test rejection'); END;")?;
            Ok(())
        }).unwrap();
        assert!(db.record_document_refresh_failure_check(1,30,"SCRAPE_TIMEOUT",&json!({"reason":"SCRAPE_TIMEOUT"})).is_err());
        let latest=db.latest_document_source_check(1).unwrap().unwrap();
        assert_eq!(latest["evidence"]["reason"],"SOURCE_WRITES_PAUSED");
        assert_eq!(latest["checked_at_ms"],20);
        db.with_conn(|c| {
            let row=c.query_row("SELECT last_refresh_checked_at_ms,last_refresh_error,full_content FROM bake_documents WHERE id=1",[],
                |r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?)))?;
            assert_eq!(row,(20,"SOURCE_WRITES_PAUSED".into(),"preserved body".into()));
            assert_eq!(c.query_row("SELECT COUNT(*) FROM bake_document_source_checks",[],|r|r.get::<_,i64>(0))?,1);
            Ok(())
        }).unwrap();
    }

    #[test]
    fn document_source_health_counts_mismatch_occurrences_in_window() {
        let db=StorageManager::open_in_memory().unwrap();
        assert_eq!(db.document_source_health(100,200).unwrap()["version_mismatches"],json!([]));
        db.with_conn(|c| {
            for (at,count) in [(99,100),(100,2),(200,3),(201,100)] {
                c.execute("INSERT INTO document_source_mismatch_events
                    (document_id,component,reason,expected_snapshot_id,observed_snapshot_id,occurrences,observed_at)
                    VALUES(953,'rag','snapshot_mismatch',61,60,?1,?2)",params![count,at])?;
            }
            Ok(())
        }).unwrap();
        let report=db.document_source_health(100,200).unwrap();
        assert_eq!(report["version_mismatches"],json!([
            {"component":"rag","reason":"snapshot_mismatch","occurrences":5}
        ]));
        assert!(!report["version_mismatches"].to_string().contains("953"));
    }

    #[test]
    fn document_queue_duplicate_health_uses_creation_cohort_and_groups_documents() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| {
            for id in [1,2] {
                c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at)
                    VALUES(?1,'test','document',1,1)",params![id])?;
            }
            Ok(())
        }).unwrap();
        assert_eq!(db.document_source_health(100,200).unwrap()["observations"]["duplicate_enqueues_by_document"],json!([]));
        for (id,fingerprint,created,duplicate) in [
            (1,"private-a",100,110),(1,"private-b",200,210),
            (2,"private-before",99,150),(2,"private-after",201,220),
        ] {
            assert!(db.enqueue_document_refresh_observation(id,fingerprint,None,created).unwrap());
            assert!(!db.enqueue_document_refresh_observation(id,fingerprint,None,duplicate).unwrap());
        }
        let report=db.document_source_health(100,200).unwrap();
        assert_eq!(report["observations"]["duplicate_enqueue_count"],2);
        assert_eq!(report["observations"]["duplicate_enqueues_by_document"],json!([
            {"document_id":1,"duplicate_enqueue_count":2}
        ]));
        assert_eq!(report["prequeue_coalescing"]["total"],0);
        assert!(!report.to_string().contains("private"));
    }
    #[test]
    fn health_counts_coverage_separately_and_omits_missing_timings_and_private_text() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| {c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(1,'test','document',1,1)",[])?;Ok(())}).unwrap();
        let empty=db.document_source_health(100,200).unwrap();
        assert_eq!(empty["checks"]["total"],0);
        assert!(empty["checks"]["mean_execution_ms"].is_null());
        for (at,evidence) in [
            (99,json!({"coverage":"complete","execution_ms":9000})),
            (110,json!({"coverage":"complete","execution_ms":100})),
            (120,json!({"coverage":"partial","execution_ms":300})),
            (130,json!({"coverage":"failed","reason":"SCRAPE_TIMEOUT","secret":"private-source-text"})),
            (140,json!({"coverage":"failed","execution_ms":"500","reason":"private-source-text"})),
            (201,json!({"coverage":"complete","execution_ms":9000})),
        ] {db.record_document_source_check(1,None,at,&evidence).unwrap();}
        db.enqueue_document_refresh_observation(1,"private-fingerprint",None,125).unwrap();
        let report=db.document_source_health(100,200).unwrap();
        assert_eq!(report["checks"]["total"],4);
        assert_eq!(report["checks"]["complete"],1);
        assert_eq!(report["checks"]["partial"],1);
        assert_eq!(report["checks"]["failed"],2);
        assert_eq!(report["checks"]["budget_or_timeout"],1);
        assert_eq!(report["checks"]["timed_samples"],2);
        assert_eq!(report["checks"]["mean_execution_ms"],200.0);
        assert_eq!(report["observations"]["pending"],1);
        assert_eq!(report["observations"]["oldest_pending_age_ms"],75);
        assert!(!report.to_string().contains("private"));
    }
    #[test]
    fn document_block_candidate_counts_do_not_claim_unobserved_coverage() {
        let prose="缓存更新前校验来源版本。";
        let shell="知识库 首页 目录 收藏 分享 编辑 全部暂停";
        let body=format!("{prose}\n{shell}\n42");
        let structured=json!({"document_body":{"blocks":[
            {"type":"paragraph","text":prose},{"type":"paragraph","text":shell},
            {"type":"table_cell","text":"42"}]}});
        let evidence=build_document_source_evidence(&structured,&body,"complete",0);
        assert_eq!(evidence["substantive_block_count"],2);
        assert_eq!(evidence["blocks"][1]["body_candidate"],false);
        assert_eq!(evidence["blocks"][2]["body_candidate"],true);
        assert_eq!(evidence["substantive_block_rule"],"aligned-non-shell.v1");
        let partial=build_document_source_evidence(&structured,prose,"partial",0);
        assert!(partial["substantive_block_count"].is_null());
        assert_eq!(partial["unmatched_blocks"],2);
        let absent=build_document_source_evidence(&json!({}),prose,"partial",0);
        assert!(absent["substantive_block_count"].is_null());
        assert!(!evidence.to_string().contains(prose));
    }
    #[test]
    fn source_check_references_only_redacted_snapshot_and_whitelists_metadata() {
        let text="甲\n\n乙，正文 [已过滤]\n表头";
        let input=json!({"completeness":{"reached_end":true,"segment_count":4,"secret":"never persist"},"document_body":{"version":"document-body.v3","matching_passes":2,
            "excluded_block_count":4,"exclusion_scope":"final_body_root",
            "secret":"never persist","blocks":[
                {"text":"甲 乙，正文","type":"paragraph","page":0,"key":"secret-key"},
                {"text":"credential:secret-value","type":"paragraph"},
                {"text":"表头","type":"table_cell","row":0,"column":1}]}});
        let result=build_document_source_evidence(&input,text,"partial",1);
        assert_eq!(result["block_reference_count"],2);
        assert_eq!(result["body_character_count"],text.chars().count());
        assert!(result.get("substantive_block_count").unwrap().is_null());
        assert_eq!(result["unmatched_blocks"],1);
        assert_eq!(result["excluded_block_count"],4);
        assert_eq!(result["exclusion_scope"],"final_body_root");
        assert_eq!(result["completeness_evidence"]["reached_end"],true);
        assert_eq!(result["completeness_evidence"]["segment_count"],4);
        assert!(!result.to_string().contains("secret"));
        let first=&result["blocks"][0];
        assert_eq!(&text[first["start_byte"].as_u64().unwrap() as usize..first["end_byte"].as_u64().unwrap() as usize],"甲\n\n乙，正文");
        assert_eq!(result["blocks"][1]["type"],"table_cell");
    }
    #[test]
    fn source_checks_append_even_when_snapshot_is_reused() {
        let dir=tempfile::tempdir().unwrap();
        let db=StorageManager::open(&dir.path().join("checks.db")).unwrap();
        db.with_conn(|c| {c.execute("INSERT INTO bake_documents(title,doc_type,created_at,updated_at) VALUES('test','document',1,1)",[])?;Ok(())}).unwrap();
        db.with_conn(|c| {
            c.execute("INSERT INTO bake_document_source_snapshots(id,document_id,source_url,page_title,content_text,content_hash,completeness_status,collected_at)
                VALUES(7,1,'https://example.com/document/a','test','同一份正文','same','partial',1)",[])?;
            c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(2,'other','document',1,1)",[])?;
            Ok(())
        }).unwrap();
        let a=db.record_document_source_check(1,Some(7),100,&json!({"coverage":"partial","extractor_version":"document-body.v2"})).unwrap();
        let b=db.record_document_source_check(1,Some(7),101,&json!({"coverage":"complete","extractor_version":"document-body.v3"})).unwrap();
        assert_ne!(a,b);
        assert_eq!(db.latest_document_source_check(1).unwrap().unwrap()["id"],b);
        assert_eq!(db.latest_document_source_check(1).unwrap().unwrap()["snapshot_id"],7);
        assert!(db.record_document_source_check(2,Some(7),102,&json!({})).is_err());
        assert!(db.record_document_source_check(1,Some(999),102,&json!({})).is_err());
        db.with_conn(|c| {
            let count:i64=c.query_row("SELECT COUNT(*) FROM bake_document_source_checks WHERE snapshot_id=7",[],|r|r.get(0))?;
            assert_eq!(count,2);
            let old:String=c.query_row("SELECT evidence_json FROM bake_document_source_checks WHERE id=?1",params![a],|r|r.get(0))?;
            assert_eq!(serde_json::from_str::<Value>(&old).unwrap()["coverage"],"partial");
            Ok(())
        }).unwrap();

    }
}
