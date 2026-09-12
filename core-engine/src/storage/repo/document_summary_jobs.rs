use rusqlite::{params, OptionalExtension};
use crate::{services::document_refresh::{DocumentRefreshConfig,DOCUMENT_REFRESH_CONFIG_KEY},
    storage::{StorageManager,error::StorageError,document_identity::{canonical_document_identity,canonical_document_title_identity}}};

#[derive(Debug,Clone,serde::Serialize)]
pub struct DocumentSummaryJob {
    pub document_id:i64,
    pub source_snapshot_id:i64,
    pub expected_updated_at:i64,
    pub content_text:String,
    #[serde(skip)] pub lease_id:String,
    #[serde(skip)] pub attempts:i64,
}

#[cfg(test)]
mod tests {
    use super::*;
    fn seed(db:&StorageManager) {
        db.with_conn(|c| {
            c.execute_batch("INSERT INTO bake_documents(id,title,doc_type,full_content,created_at,updated_at)
                VALUES(1,'summary source','document','verified body',1,1);
                INSERT INTO bake_document_source_snapshots(id,document_id,source_url,page_title,content_text,content_hash,completeness_status,identity_match,collected_at)
                VALUES(61,1,'https://example.com/doc','summary source','verified body','hash','complete',1,1);
                INSERT INTO bake_document_source_heads VALUES(1,61,1);")?;Ok(())
        }).unwrap();
    }
    #[test]
    fn blank_unbound_summary_offers_explicit_regeneration_instead_of_idle_pending() {
        for summary in ["", "   ", "\n\t\u{3000}"] {
            let db=StorageManager::open_in_memory().unwrap();seed(&db);
            db.with_conn(|c| {c.execute("UPDATE bake_documents SET summary=?1 WHERE id=1",[summary])?;Ok(())}).unwrap();
            // Non-NULL text is never overwritten by the background worker.
            assert!(db.claim_document_summary_job(10).unwrap().is_none());
            let status=db.document_summary_status(1,1).unwrap().unwrap();
            assert_eq!(status["state"],"unverified");
            assert_eq!(status["can_regenerate"],true);
            assert!(db.regenerate_document_summary(1,1,10).unwrap());
            db.with_conn(|c| {
                let archived:String=c.query_row("SELECT record_json FROM document_summary_versions",[],|r|r.get(0))?;
                assert_eq!(serde_json::from_str::<serde_json::Value>(&archived).unwrap()["summary"],summary);
                Ok(())
            }).unwrap();
            assert!(db.claim_document_summary_job(crate::storage::db::current_ts_ms()).unwrap().is_some());
        }
    }

    #[test]
    fn document_summary_schedule_audits_only_enabled_invalid_sources() {
        for mutation in ["UPDATE bake_document_source_snapshots SET content_text='wrong body'",
            "UPDATE bake_document_source_snapshots SET identity_match=0",
            "UPDATE bake_document_source_snapshots SET completeness_status='partial'",
            "INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(2,'other','document',1,1); UPDATE bake_document_source_snapshots SET document_id=2"] {
            let db=StorageManager::open_in_memory().unwrap();seed(&db);
            db.with_conn(|c|{c.execute_batch(mutation)?;Ok(())}).unwrap();
            db.upsert_preference("runtime.capture_enabled","false","user",1.0).unwrap();
            assert!(db.claim_document_summary_job(100).unwrap().is_none());
            db.upsert_preference("runtime.capture_enabled","true","user",1.0).unwrap();
            db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,r#"{"rollout_document_ids":[]}"#,"user",1.0).unwrap();
            assert!(db.claim_document_summary_job(101).unwrap().is_none());
            db.with_conn(|c|{assert_eq!(c.query_row("SELECT COUNT(*) FROM document_source_mismatch_events",[],|r|r.get::<_,i64>(0))?,0);Ok(())}).unwrap();
            db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,"{}","user",1.0).unwrap();
            assert!(db.claim_document_summary_job(102).unwrap().is_none());
            db.with_conn(|c| {
                let event:(String,String,i64)=c.query_row("SELECT component,reason,occurrences FROM document_source_mismatch_events",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
                assert_eq!(event,("summary_schedule".into(),"head_invalid".into(),1));
                assert_eq!(c.query_row("SELECT COUNT(*) FROM document_summary_jobs",[],|r|r.get::<_,i64>(0))?,0);
                c.execute_batch("CREATE TRIGGER reject_schedule_audit BEFORE INSERT ON document_source_mismatch_events
                    BEGIN SELECT RAISE(ABORT,'audit failed'); END;
                    INSERT INTO bake_documents(id,title,doc_type,full_content,created_at,updated_at) VALUES(3,'valid','document','valid source',1,1);
                    INSERT INTO bake_document_source_snapshots(id,document_id,source_url,page_title,content_text,content_hash,completeness_status,identity_match,collected_at)
                    VALUES(63,3,'https://example.com/valid','valid','valid source','valid-hash','complete',1,1);
                    INSERT INTO bake_document_source_heads VALUES(3,63,1);")?;Ok(())
            }).unwrap();
            assert_eq!(db.claim_document_summary_job(103).unwrap().unwrap().document_id,3);
            assert_eq!(db.document_source_health(0,103).unwrap()["version_mismatches"],
                serde_json::json!([{"component":"summary_schedule","reason":"head_invalid","occurrences":1}]));
        }
    }

    #[test]
    fn document_summary_regeneration_preserves_unknown_text_and_rejects_stale_requests() {
        let db=StorageManager::open_in_memory().unwrap();seed(&db);
        db.with_conn(|c| {c.execute("UPDATE bake_documents SET summary='original user or legacy text' WHERE id=1",[])?;Ok(())}).unwrap();
        assert!(db.claim_document_summary_job(10).unwrap().is_none());
        assert_eq!(db.document_summary_status(1,1).unwrap().unwrap()["can_regenerate"],true);
        assert!(!db.regenerate_document_summary(1,2,10).unwrap());
        assert!(db.regenerate_document_summary(1,1,10).unwrap());
        assert!(!db.regenerate_document_summary(1,1,10).unwrap());
        db.with_conn(|c| {
            let previous:String=c.query_row("SELECT record_json FROM document_summary_versions",[],|r|r.get(0))?;
            let previous:serde_json::Value=serde_json::from_str(&previous).unwrap();
            assert_eq!(previous["summary"],"original user or legacy text");
            assert_eq!(previous["updated_at"],1);
            assert!(previous["summary_source_snapshot_id"].is_null());
            assert_eq!(c.query_row("SELECT count(*) FROM document_summary_versions",[],|r|r.get::<_,i64>(0))?,1);
            Ok(())
        }).unwrap();
        let job=db.claim_document_summary_job(crate::storage::db::current_ts_ms()).unwrap().unwrap();
        assert!(db.publish_document_summary_job(&job,"new verified summary").unwrap());
        let revision=db.get_bake_document(1).unwrap().unwrap().updated_at;
        assert!(!db.regenerate_document_summary(1,revision,revision+1).unwrap());
        assert_eq!(db.document_summary_status(1,revision).unwrap().unwrap()["can_regenerate"],false);
    }

    #[test]
    fn document_summary_regeneration_rolls_back_when_archive_fails_and_requires_valid_body() {
        let db=StorageManager::open_in_memory().unwrap();seed(&db);
        db.with_conn(|c| {c.execute_batch("UPDATE bake_documents SET summary='keep me' WHERE id=1;
            CREATE TRIGGER reject_summary_archive BEFORE INSERT ON document_summary_versions
            BEGIN SELECT RAISE(ABORT,'archive failed'); END;")?;Ok(())}).unwrap();
        assert!(db.regenerate_document_summary(1,1,10).is_err());
        assert_eq!(db.get_bake_document(1).unwrap().unwrap().summary.as_deref(),Some("keep me"));
        assert_eq!(db.get_bake_document(1).unwrap().unwrap().updated_at,1);
        db.with_conn(|c| {c.execute_batch("DROP TRIGGER reject_summary_archive;
            UPDATE bake_document_source_snapshots SET content_text='different body' WHERE id=61;")?;Ok(())}).unwrap();
        assert!(!db.regenerate_document_summary(1,1,10).unwrap());
        assert_eq!(db.document_summary_status(1,1).unwrap().unwrap()["can_regenerate"],false);
    }

    #[test]
    fn document_summary_jobs_reopen_recover_expired_lease_and_bound_retries() {
        let dir=tempfile::tempdir().unwrap();
        let path=dir.path().join("summary.db");
        let db=StorageManager::open(&path).unwrap();seed(&db);
        let now=crate::storage::db::current_ts_ms();
        let first=db.claim_document_summary_job(now).unwrap().unwrap();
        assert!(db.claim_document_summary_job(now+1).unwrap().is_none());
        drop(db);
        let db=StorageManager::open(&path).unwrap();
        let second=db.claim_document_summary_job(now+400_000).unwrap().unwrap();
        assert_eq!(second.attempts,2);
        assert_ne!(first.lease_id,second.lease_id);
        assert!(!db.publish_document_summary_job(&first,"expired result").unwrap());
        assert!(!db.finish_document_summary_job(&first,now,0,3,false,false,"old worker").unwrap());
        assert!(db.finish_document_summary_job(&second,now+400_000,1000,3,false,false,"provider raw body").unwrap());
        assert!(db.claim_document_summary_job(now+400_999).unwrap().is_none());
        let third=db.claim_document_summary_job(now+401_000).unwrap().unwrap();
        assert_eq!(third.attempts,3);
        assert!(db.finish_document_summary_job(&third,now+402_000,0,3,false,false,"failure").unwrap());
        assert!(db.claim_document_summary_job(now+900_000).unwrap().is_none());
        db.with_conn(|c| {
            assert_eq!(c.query_row("SELECT last_error FROM document_summary_jobs",[],|r|r.get::<_,String>(0))?,"SUMMARY_GENERATION_FAILED");
            assert_eq!(c.query_row("SELECT state FROM document_summary_jobs",[],|r|r.get::<_,String>(0))?,"blocked");Ok(())
        }).unwrap();
    }
    #[test]
    fn document_summary_status_uses_current_source_and_revision() {
        let db=StorageManager::open_in_memory().unwrap();seed(&db);
        assert_eq!(db.document_summary_status(1,1).unwrap().unwrap()["state"],"pending");
        let job=db.claim_document_summary_job(crate::storage::db::current_ts_ms()).unwrap().unwrap();
        assert_eq!(db.document_summary_status(1,1).unwrap().unwrap()["state"],"running");
        db.upsert_preference("runtime.capture_enabled","false","user",1.0).unwrap();
        assert_eq!(db.document_summary_status(1,1).unwrap().unwrap()["paused"],true);
        db.upsert_preference("runtime.capture_enabled","true","user",1.0).unwrap();
        db.with_conn(|c| {c.execute("UPDATE bake_documents SET summary='user text' WHERE id=1",[])?;Ok(())}).unwrap();
        assert!(!db.publish_document_summary_job(&job,"late automatic summary").unwrap());
        db.with_conn(|c| {c.execute("UPDATE bake_documents SET summary=NULL WHERE id=1",[])?;Ok(())}).unwrap();
        assert!(db.publish_document_summary_job(&job,"verified summary").unwrap());
        let current=db.get_bake_document(1).unwrap().unwrap();
        assert_eq!(db.document_summary_status(1,current.updated_at).unwrap().unwrap()["state"],"ready");
        assert_eq!(db.document_summary_status(1,1).unwrap().unwrap()["state"],"pending");
        db.with_conn(|c| {c.execute("UPDATE bake_documents SET summary_source_snapshot_id=NULL WHERE id=1",[])?;Ok(())}).unwrap();
        assert_eq!(db.document_summary_status(1,current.updated_at).unwrap().unwrap()["state"],"unverified");
        db.with_conn(|c| {c.execute("UPDATE bake_documents SET full_content=NULL WHERE id=1",[])?;Ok(())}).unwrap();
        assert_eq!(db.document_summary_status(1,current.updated_at).unwrap().unwrap()["state"],"unverified");
    }

    #[test]
    fn document_summary_explicit_retry_preserves_failed_run_and_is_idempotent() {
        let db=StorageManager::open_in_memory().unwrap();seed(&db);
        let now=crate::storage::db::current_ts_ms();
        let job=db.claim_document_summary_job(now).unwrap().unwrap();
        assert!(!db.retry_document_summary(1,now).unwrap());
        db.finish_document_summary_job(&job,now,0,1,false,false,"SUMMARY_INVALID_RESULT").unwrap();
        db.with_conn(|c| {c.execute_batch("CREATE TRIGGER fail_summary_retry_audit BEFORE INSERT ON document_summary_retry_events
            BEGIN SELECT RAISE(ABORT,'audit unavailable'); END")?;Ok(())}).unwrap();
        assert!(db.retry_document_summary(1,now+1).is_err());
        db.with_conn(|c| {
            assert_eq!(c.query_row("SELECT state FROM document_summary_jobs",[],|r|r.get::<_,String>(0))?,"blocked");
            c.execute_batch("DROP TRIGGER fail_summary_retry_audit")?;Ok(())
        }).unwrap();
        assert!(db.retry_document_summary(1,now+2).unwrap());
        assert!(!db.retry_document_summary(1,now+2).unwrap());
        db.with_conn(|c| {
            assert_eq!(c.query_row("SELECT COUNT(*) FROM document_summary_retry_events",[],|r|r.get::<_,i64>(0))?,1);
            let previous:(i64,String,String)=c.query_row("SELECT previous_attempts,previous_state,previous_error FROM document_summary_retry_events",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
            assert_eq!(previous,(1,"blocked".into(),"SUMMARY_INVALID_RESULT".into()));Ok(())
        }).unwrap();
        let resumed=db.claim_document_summary_job(now+3).unwrap().unwrap();
        assert_eq!(resumed.attempts,1);
        assert!(!db.publish_document_summary_job(&job,"old lease").unwrap());
        assert!(db.publish_document_summary_job(&resumed,"verified summary").unwrap());
        assert!(!db.retry_document_summary(1,now+4).unwrap());
    }

    #[test]
    fn document_summary_expired_result_can_retry_unchanged_source() {
        let db=StorageManager::open_in_memory().unwrap();seed(&db);
        let now=crate::storage::db::current_ts_ms();
        let job=db.claim_document_summary_job(now-400_000).unwrap().unwrap();
        assert!(!db.publish_document_summary_job(&job,"late result").unwrap());
        assert!(db.finish_document_summary_job(&job,now,0,3,false,false,"SUMMARY_SOURCE_CHANGED").unwrap());
        db.with_conn(|c| {assert_eq!(c.query_row("SELECT count(*) FROM document_source_mismatch_events",[],|r|r.get::<_,i64>(0))?,0);Ok(())}).unwrap();
        let retry=db.claim_document_summary_job(now+1).unwrap().unwrap();
        assert_eq!(retry.attempts,2);
        db.with_conn(|c| {c.execute("DELETE FROM bake_document_source_heads WHERE document_id=1",[])?;Ok(())}).unwrap();
        assert!(!db.publish_document_summary_job(&retry,"removed source").unwrap());
        assert!(db.finish_document_summary_job(&retry,now+2,0,3,false,false,"SUMMARY_SOURCE_CHANGED").unwrap());
        assert!(!db.finish_document_summary_job(&retry,now+3,0,3,false,false,"SUMMARY_SOURCE_CHANGED").unwrap());
        db.with_conn(|c| {
            let event:(String,i64,Option<i64>,i64)=c.query_row("SELECT component,expected_snapshot_id,observed_snapshot_id,occurrences FROM document_source_mismatch_events",[],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
            assert_eq!(event,("summary_write".into(),61,None,1));
            assert_eq!(c.query_row("SELECT count(*) FROM document_source_mismatch_events",[],|r|r.get::<_,i64>(0))?,1);
            assert_eq!(c.query_row("SELECT state FROM document_summary_jobs",[],|r|r.get::<_,String>(0))?,"superseded");Ok(())
        }).unwrap();
    }

    #[test]
    fn summary_source_audit_handles_same_head_body_change_without_retrying_stale_input() {
        for mutation in ["UPDATE bake_documents SET full_content='new body' WHERE id=1",
            "UPDATE bake_document_source_snapshots SET completeness_status='partial' WHERE id=61"] {
            let db=StorageManager::open_in_memory().unwrap();seed(&db);
            let now=crate::storage::db::current_ts_ms();
            let job=db.claim_document_summary_job(now).unwrap().unwrap();
            db.with_conn(|c| {c.execute_batch(mutation)?;Ok(())}).unwrap();
            assert!(!db.publish_document_summary_job(&job,"stale summary").unwrap());
            assert!(db.finish_document_summary_job(&job,now+1,0,3,false,false,"SUMMARY_SOURCE_CHANGED").unwrap());
            db.with_conn(|c| {
                assert_eq!(c.query_row("SELECT state FROM document_summary_jobs",[],|r|r.get::<_,String>(0))?,"superseded");
                let event:(i64,i64)=c.query_row("SELECT expected_snapshot_id,observed_snapshot_id FROM document_source_mismatch_events",[],|r|Ok((r.get(0)?,r.get(1)?)))?;
                assert_eq!(event,(61,61));Ok(())
            }).unwrap();
            assert!(db.get_bake_document(1).unwrap().unwrap().summary.is_none());
            assert_eq!(db.document_source_health(now,now+2).unwrap()["version_mismatches"],serde_json::json!([
                {"component":"summary_write","reason":"summary_version_mismatch","occurrences":1}
            ]));
        }
    }

    #[test]
    fn summary_audit_failure_does_not_publish_or_retry_stale_source() {
        let db=StorageManager::open_in_memory().unwrap();seed(&db);
        let now=crate::storage::db::current_ts_ms();
        let job=db.claim_document_summary_job(now).unwrap().unwrap();
        db.with_conn(|c| {c.execute_batch("DELETE FROM bake_document_source_heads WHERE document_id=1;
            CREATE TRIGGER fail_mismatch_audit BEFORE INSERT ON document_source_mismatch_events
            BEGIN SELECT RAISE(ABORT,'unavailable'); END;")?;Ok(())}).unwrap();
        assert!(!db.publish_document_summary_job(&job,"old result").unwrap());
        assert!(db.finish_document_summary_job(&job,now+1,0,3,false,false,"SUMMARY_SOURCE_CHANGED").unwrap());
        assert!(db.get_bake_document(1).unwrap().unwrap().summary.is_none());
        db.with_conn(|c| {assert_eq!(c.query_row("SELECT state FROM document_summary_jobs",[],|r|r.get::<_,String>(0))?,"superseded");Ok(())}).unwrap();
    }

    #[test]
    fn document_summary_jobs_pause_and_new_revision_restore_eligibility() {
        let db=StorageManager::open_in_memory().unwrap();seed(&db);
        let now=crate::storage::db::current_ts_ms();
        db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,r#"{"rollout_document_ids":[]}"#,"user",1.0).unwrap();
        assert!(db.claim_document_summary_job(now).unwrap().is_none());
        db.upsert_preference(DOCUMENT_REFRESH_CONFIG_KEY,"{}","user",1.0).unwrap();
        let job=db.claim_document_summary_job(now).unwrap().unwrap();
        db.upsert_preference("runtime.capture_enabled","false","user",1.0).unwrap();
        assert!(db.publish_document_summary_job(&job,"must remain paused").is_err());
        assert!(db.claim_document_summary_job(now+400_000).unwrap().is_none());
        db.upsert_preference("runtime.capture_enabled","true","user",1.0).unwrap();
        db.finish_document_summary_job(&job,now,0,3,false,true,"SUMMARY_PAUSED").unwrap();
        let resumed=db.claim_document_summary_job(now+1).unwrap().unwrap();
        assert_eq!(resumed.attempts,1);
        assert!(db.publish_document_summary_job(&resumed,"verified summary").unwrap());
        assert!(db.claim_document_summary_job(now+2).unwrap().is_none());
        db.with_conn(|c| {
            assert_eq!(c.query_row("SELECT state FROM document_summary_jobs",[],|r|r.get::<_,String>(0))?,"completed");
            assert_eq!(c.query_row("SELECT summary_generation_version FROM bake_documents WHERE id=1",[],|r|r.get::<_,String>(0))?,"document-summary.v1");
            c.execute("UPDATE bake_documents SET summary=NULL,updated_at=updated_at+1 WHERE id=1",[])?;Ok(())
        }).unwrap();
        assert_eq!(db.claim_document_summary_job(now+3).unwrap().unwrap().attempts,1);
    }
}

impl StorageManager {
    pub fn document_summary_status(&self,id:i64,expected_updated_at:i64)->Result<Option<serde_json::Value>,StorageError> {
        self.with_conn(|conn| {
            let row:Option<(i64,Option<String>,bool,bool,Option<String>,Option<String>,i64,i64,Option<String>,String,Option<String>)>=conn.query_row(
                "SELECT d.updated_at,d.summary,
                    COALESCE(s.document_id=d.id AND s.identity_match=1 AND s.completeness_status='complete' AND s.content_text=d.full_content,0),
                    COALESCE(d.summary_source_snapshot_id=h.snapshot_id,0),d.summary_generation_version,
                    j.state,COALESCE(j.attempts,0),COALESCE(j.next_attempt_at,0),j.last_error,d.title,d.source_url
                 FROM bake_documents d JOIN bake_document_source_heads h ON h.document_id=d.id
                 JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id
                 LEFT JOIN document_summary_jobs j ON j.document_id=d.id AND j.source_snapshot_id=h.snapshot_id
                    AND j.expected_updated_at=d.updated_at
                 WHERE d.id=?1 AND d.deleted_at IS NULL",params![id],
                |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?,r.get(7)?,r.get(8)?,r.get(9)?,r.get(10)?))).optional()?;
            let Some((revision,summary,valid,bound,version,job_state,attempts,next,error,title,url))=row else{return Ok(None);};
            let raw:Option<String>=conn.query_row("SELECT value FROM user_preferences WHERE key=?1",[DOCUMENT_REFRESH_CONFIG_KEY],|r|r.get(0)).optional()?;
            let identity=url.as_deref().and_then(canonical_document_identity)
                .or_else(||canonical_document_title_identity(&title)).unwrap_or_default();
            let enabled=raw.as_deref().map(DocumentRefreshConfig::parse).transpose()
                .map(|c| {let c=c.unwrap_or_default(); c.enabled && c.automatic_enabled && c.permits_source_write(id)
                    && c.permits_automatic_document_write(&identity)}).unwrap_or(false);
            let capture_enabled:Option<String>=conn.query_row("SELECT value FROM user_preferences WHERE key='runtime.capture_enabled'",[],|r|r.get(0)).optional()?;
            let paused=!enabled || capture_enabled.as_deref().is_some_and(|v|v.eq_ignore_ascii_case("false"));
            let state=if revision!=expected_updated_at {"pending"}
                else if !valid {"unverified"}
                else if let Some(text)=summary.as_deref() {
                    if bound && !text.trim().is_empty() {"ready"}else{"unverified"}
                } else {match job_state.as_deref() {Some("blocked")=>"blocked",Some("running")=>"running",_=>"pending"}};
            Ok(Some(serde_json::json!({"state":state,"paused":paused,"attempts":attempts,
                "next_attempt_at_ms":next,"last_error":error,"generation_version":version,
                "can_regenerate":revision==expected_updated_at && valid && !bound && summary.is_some()})))
        })
    }

    /// Explicit replacement archives unknown/manual text; background work never clears it.
    pub fn regenerate_document_summary(&self,document_id:i64,expected_revision:i64,now:i64)->Result<bool,StorageError> {
        self.with_conn(|conn| {
            let tx=conn.unchecked_transaction()?;
            let previous:Option<String>=tx.query_row(
                "SELECT json_object('summary',d.summary,'summary_source_snapshot_id',d.summary_source_snapshot_id,
                    'summary_generation_version',d.summary_generation_version,'updated_at',d.updated_at)
                 FROM bake_documents d JOIN bake_document_source_heads h ON h.document_id=d.id
                 JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id AND s.document_id=d.id
                 WHERE d.id=?1 AND d.updated_at=?2 AND d.deleted_at IS NULL AND d.summary IS NOT NULL
                 AND COALESCE(d.summary_source_snapshot_id=h.snapshot_id,0)=0
                 AND s.identity_match=1 AND s.completeness_status='complete' AND s.content_text=d.full_content",
                params![document_id,expected_revision],|r|r.get(0)).optional()?;
            let Some(previous)=previous else{return Ok(false);};
            tx.execute("INSERT INTO document_summary_versions(version_key,document_id,record_json,saved_at,reason)
                VALUES(?1,?2,?3,?4,'explicit_regeneration')",
                params![uuid::Uuid::new_v4().to_string(),document_id,previous,now])?;
            tx.execute("UPDATE bake_documents SET summary=NULL,summary_source_snapshot_id=NULL,
                summary_generation_version=NULL,updated_at=MAX(?3,updated_at+1)
                WHERE id=?1 AND updated_at=?2",params![document_id,expected_revision,now])?;
            tx.execute("INSERT OR IGNORE INTO vector_deletion_queue(qdrant_point_id,source_type,reason,enqueued_at)
                SELECT qdrant_point_id,'document','summary_regeneration',?2 FROM artifact_vector_index WHERE document_id=?1",
                params![document_id,now])?;
            tx.execute("DELETE FROM artifact_vector_index WHERE document_id=?1",[document_id])?;
            tx.execute("UPDATE document_summary_jobs SET state='superseded',lease_id=NULL,lease_until=0
                WHERE document_id=?1 AND state IN ('pending','running')",[document_id])?;
            tx.commit()?;Ok(true)
        })
    }

    /// A user-requested retry keeps the exhausted run as immutable audit evidence.
    pub fn retry_document_summary(&self,document_id:i64,now:i64)->Result<bool,StorageError> {
        self.with_conn(|conn| {
            let tx=conn.unchecked_transaction()?;
            let row:Option<(i64,i64,i64,Option<String>)>=tx.query_row(
                "SELECT j.source_snapshot_id,j.expected_updated_at,j.attempts,j.last_error
                 FROM document_summary_jobs j JOIN bake_documents d ON d.id=j.document_id
                 JOIN bake_document_source_heads h ON h.document_id=d.id AND h.snapshot_id=j.source_snapshot_id
                 JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id AND s.document_id=d.id
                 WHERE d.id=?1 AND j.state='blocked' AND d.deleted_at IS NULL AND d.summary IS NULL
                 AND j.expected_updated_at=d.updated_at AND s.identity_match=1
                 AND s.completeness_status='complete' AND s.content_text=d.full_content",
                params![document_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?))).optional()?;
            let Some((snapshot,revision,attempts,error))=row else{return Ok(false);};
            tx.execute("INSERT INTO document_summary_retry_events(document_id,source_snapshot_id,expected_updated_at,
                previous_attempts,previous_state,previous_error,requested_at,reason,previous_record_json)
                VALUES(?1,?2,?3,?4,'blocked',?5,?6,'explicit_retry',
                    (SELECT json_object('document_id',document_id,'source_snapshot_id',source_snapshot_id,
                        'expected_updated_at',expected_updated_at,'attempts',attempts,'lease_id',lease_id,
                        'lease_until',lease_until,'next_attempt_at',next_attempt_at,'state',state,'last_error',last_error)
                     FROM document_summary_jobs WHERE document_id=?1 AND source_snapshot_id=?2 AND expected_updated_at=?3))",
                params![document_id,snapshot,revision,attempts,error,now])?;
            tx.execute("UPDATE document_summary_jobs SET attempts=0,state='pending',last_error=NULL,
                lease_id=NULL,lease_until=0,next_attempt_at=?4
                WHERE document_id=?1 AND source_snapshot_id=?2 AND expected_updated_at=?3",
                params![document_id,snapshot,revision,now])?;
            tx.commit()?;Ok(true)
        })
    }

    pub fn claim_document_summary_job(&self,now:i64) -> Result<Option<DocumentSummaryJob>,StorageError> {
        self.with_conn(|conn| {
            let tx=conn.unchecked_transaction()?;
            let capture_enabled:Option<String>=tx.query_row("SELECT value FROM user_preferences WHERE key='runtime.capture_enabled'",
                [],|r|r.get(0)).optional()?;
            if capture_enabled.as_deref().is_some_and(|v|v.eq_ignore_ascii_case("false")) {return Ok(None);}
            let raw:Option<String>=tx.query_row("SELECT value FROM user_preferences WHERE key=?1",
                [DOCUMENT_REFRESH_CONFIG_KEY],|r|r.get(0)).optional()?;
            let config=match raw.as_deref().map(DocumentRefreshConfig::parse).transpose() {
                Ok(c)=>c.unwrap_or_default(),Err(_)=>return Ok(None),
            };
            if !config.enabled || !config.automatic_enabled || !config.source_writes_enabled
                || !config.automatic_document_writes_enabled { return Ok(None); }
            {
                let mut invalid=tx.prepare("SELECT d.id,h.snapshot_id,d.title,d.source_url
                    FROM bake_documents d JOIN bake_document_source_heads h ON h.document_id=d.id
                    LEFT JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id
                    WHERE d.deleted_at IS NULL AND d.summary IS NULL AND NOT COALESCE(
                        s.document_id=d.id AND s.identity_match=1 AND s.completeness_status='complete'
                        AND s.content_text=d.full_content,0)")?;
                let mut rows=invalid.query([])?;
                while let Some(row)=rows.next()? {
                    let id:i64=row.get(0)?;
                    let snapshot:i64=row.get(1)?;
                    let title:String=row.get(2)?;
                    let url:Option<String>=row.get(3)?;
                    let identity=url.as_deref().and_then(canonical_document_identity)
                        .or_else(||canonical_document_title_identity(&title)).unwrap_or_default();
                    if !config.permits_source_write(id) || !config.permits_automatic_document_write(&identity) {continue;}
                    if tx.execute("INSERT INTO document_source_mismatch_events
                        (document_id,component,reason,expected_snapshot_id,observed_snapshot_id,occurrences,observed_at)
                        VALUES(?1,'summary_schedule','head_invalid',?2,?2,1,?3)",params![id,snapshot,now]).is_err() {
                        tracing::warn!("document_summary_schedule_audit_write_failed");
                    }
                }
            }
            tx.execute("UPDATE document_summary_jobs SET state='superseded',lease_id=NULL,lease_until=0
                WHERE state IN ('pending','running') AND NOT EXISTS(
                    SELECT 1 FROM bake_documents d JOIN bake_document_source_heads h ON h.document_id=d.id
                    WHERE d.id=document_summary_jobs.document_id AND d.updated_at=document_summary_jobs.expected_updated_at
                    AND h.snapshot_id=document_summary_jobs.source_snapshot_id AND d.deleted_at IS NULL)",[])?;
            tx.execute("UPDATE document_summary_jobs SET state='blocked',last_error='SUMMARY_ATTEMPTS_EXHAUSTED',lease_id=NULL
                WHERE state='running' AND lease_until<=?1 AND attempts>=?2",params![now,config.max_attempts])?;
            let mut statement=tx.prepare("SELECT d.id,h.snapshot_id,d.updated_at,d.full_content,d.title,d.source_url,COALESCE(j.attempts,0)
                FROM bake_documents d JOIN bake_document_source_heads h ON h.document_id=d.id
                JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id AND s.document_id=d.id
                LEFT JOIN document_summary_jobs j ON j.document_id=d.id AND j.source_snapshot_id=h.snapshot_id
                    AND j.expected_updated_at=d.updated_at
                WHERE d.deleted_at IS NULL AND d.summary IS NULL AND s.identity_match=1
                    AND s.completeness_status='complete' AND s.content_text=d.full_content
                    AND COALESCE(j.attempts,0)<?2 AND COALESCE(j.next_attempt_at,0)<=?1
                    AND (j.state IS NULL OR j.state='pending' OR (j.state='running' AND j.lease_until<=?1))
                ORDER BY COALESCE(j.next_attempt_at,0),d.updated_at,d.id")?;
            let mut rows=statement.query(params![now,config.max_attempts])?;
            let mut selected=None;
            while let Some(row)=rows.next()? {
                let id:i64=row.get(0)?;
                let title:String=row.get(4)?;
                let url:Option<String>=row.get(5)?;
                let identity=url.as_deref().and_then(canonical_document_identity)
                    .or_else(||canonical_document_title_identity(&title)).unwrap_or_default();
                if !config.permits_source_write(id) || !config.permits_automatic_document_write(&identity) { continue; }
                selected=Some(DocumentSummaryJob{document_id:id,source_snapshot_id:row.get(1)?,
                    expected_updated_at:row.get(2)?,content_text:row.get(3)?,
                    lease_id:uuid::Uuid::new_v4().to_string(),attempts:row.get::<_,i64>(6)?+1});
                break;
            }
            drop(rows);drop(statement);
            if let Some(job)=selected.as_ref() {
                tx.execute("INSERT INTO document_summary_jobs
                    (document_id,source_snapshot_id,expected_updated_at,attempts,lease_id,lease_until,state)
                    VALUES(?1,?2,?3,?4,?5,?6,'running')
                    ON CONFLICT(document_id,source_snapshot_id,expected_updated_at) DO UPDATE SET
                    attempts=excluded.attempts,lease_id=excluded.lease_id,lease_until=excluded.lease_until,state='running'",
                    params![job.document_id,job.source_snapshot_id,job.expected_updated_at,job.attempts,
                        job.lease_id,now+(config.summary_execution_seconds as i64+30)*1000])?;
            }
            tx.commit()?;
            Ok(selected)
        })
    }

    pub fn finish_document_summary_job(&self,job:&DocumentSummaryJob,now:i64,
        retry_delay_ms:i64,max_attempts:i64,permanent:bool,paused:bool,reason:&str) -> Result<bool,StorageError> {
        // Only fixed worker codes may be persisted, never provider response bodies.
        let code=match reason {
            "SUMMARY_INPUT_BUDGET"|"SUMMARY_INVALID_RESULT"|"SUMMARY_SOURCE_CHANGED"|"SUMMARY_PAUSED"=>reason,
            _=>"SUMMARY_GENERATION_FAILED",
        };
        self.with_conn(|conn| {
            let tx=conn.unchecked_transaction()?;
            let current:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM bake_documents d
                JOIN bake_document_source_heads h ON h.document_id=d.id
                JOIN bake_document_source_snapshots s ON s.id=h.snapshot_id AND s.document_id=d.id
                WHERE d.id=?1 AND d.updated_at=?2 AND h.snapshot_id=?3 AND d.deleted_at IS NULL AND d.summary IS NULL
                AND s.identity_match=1 AND s.completeness_status='complete' AND s.content_text=d.full_content)",
                params![job.document_id,job.expected_updated_at,job.source_snapshot_id],|r|r.get(0))?;
            let state=if code=="SUMMARY_SOURCE_CHANGED" && !current {"superseded"}
                else if !paused && (permanent || job.attempts>=max_attempts) {"blocked"} else {"pending"};
            let changed=tx.execute("UPDATE document_summary_jobs SET state=?5,last_error=?6,next_attempt_at=?7,
                lease_id=NULL,lease_until=0,attempts=MAX(0,attempts-?8)
                WHERE document_id=?1 AND source_snapshot_id=?2 AND expected_updated_at=?3 AND lease_id=?4 AND state='running'",
                params![job.document_id,job.source_snapshot_id,job.expected_updated_at,job.lease_id,state,code,
                    now+retry_delay_ms,i64::from(paused)])?>0;
            if changed && code=="SUMMARY_SOURCE_CHANGED" && !current {
                // Only the owning job records this rejection once. Lease expiry alone
                // is a retry, not evidence that the document source changed.
                if tx.execute("INSERT INTO document_source_mismatch_events
                    (document_id,component,reason,expected_snapshot_id,observed_snapshot_id,occurrences,observed_at)
                    VALUES(?1,'summary_write','summary_version_mismatch',?2,
                        (SELECT snapshot_id FROM bake_document_source_heads WHERE document_id=?1),1,?3)",
                    params![job.document_id,job.source_snapshot_id,now]).is_err() {
                    tracing::warn!("document_summary_mismatch_audit_write_failed");
                }
            }
            tx.commit()?;
            Ok(changed)
        })
    }
}
