use crate::storage::{error::StorageError, StorageManager};
use rusqlite::{params, OptionalExtension};

pub struct DocumentRefreshJob {
    pub document_id: i64,
    pub lease_id: String,
    pub attempts: i64,
}

impl StorageManager {
    pub fn document_refresh_lease_active(&self, lease_id:&str) -> Result<bool,StorageError> {
        self.with_conn(|c| Ok(c.query_row("SELECT EXISTS(SELECT 1 FROM bake_document_refresh_observations WHERE lease_id=?1 AND state='running')",params![lease_id],|r|r.get(0))?))
    }
    pub fn cancel_document_refresh_observations(&self, document_id:i64, now:i64) -> Result<usize,StorageError> {
        self.with_conn(|c| {
            let tx=c.unchecked_transaction()?;
            tx.execute("UPDATE bake_document_refresh_attempt_metrics
                SET outcome='cancelled',finished_at=?2,execution_wall_ms=MAX(0,?2-started_at)
                WHERE document_id=?1 AND outcome='running'",params![document_id,now])?;
            let changed=tx.execute("UPDATE bake_document_refresh_observations
            SET state='blocked',last_error='SOURCE_REFRESH_CANCELLED',lease_id=NULL,lease_until=0,updated_at=?2
            WHERE document_id=?1 AND state IN ('pending','running')",params![document_id,now])?;
            tx.commit()?;
            Ok(changed)
        })
    }
    /// Prefer unfinished work over old successes, and the active lease over queued work.
    pub fn document_refresh_status(&self, document_id: i64) -> Result<Option<serde_json::Value>, StorageError> {
        self.with_conn(|conn| Ok(conn.query_row(
            "SELECT state,attempts,next_attempt_at,last_error,updated_at FROM bake_document_refresh_observations
             WHERE document_id=?1 ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'pending' THEN 1
             WHEN 'blocked' THEN 2 ELSE 3 END, updated_at DESC,id DESC LIMIT 1",
            params![document_id], |r| Ok(serde_json::json!({
                "state": r.get::<_,String>(0)?, "attempts": r.get::<_,i64>(1)?,
                "next_attempt_at_ms": r.get::<_,i64>(2)?, "last_error": r.get::<_,Option<String>>(3)?,
                "updated_at_ms": r.get::<_,i64>(4)?
            }))).optional()?))
    }

    /// A successful fresh check also resolves older blocked observations. It
    /// must not acknowledge observations arriving after that check began.
    pub fn acknowledge_document_observations(&self, document_id: i64, snapshot_id: i64,
        checked_at: i64) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            conn.execute("UPDATE bake_document_refresh_observations
                SET state='completed',last_error=NULL,checked_snapshot_id=?2,updated_at=?3
                WHERE document_id=?1 AND created_at<=?3 AND state IN ('pending','blocked')
                AND EXISTS(SELECT 1 FROM bake_document_source_snapshots s WHERE s.id=?2
                    AND s.document_id=?1 AND s.completeness_status='complete' AND s.identity_match=1)",
                params![document_id,snapshot_id,checked_at])?;
            Ok(())
        })
    }
    pub fn document_refresh_observation_state(
        &self,
        document_id: i64,
        fingerprint: &str,
    ) -> Result<Option<String>, StorageError> {
        self.with_conn(|conn| Ok(conn.query_row("SELECT state FROM bake_document_refresh_observations WHERE document_id=?1 AND fingerprint=?2",
            params![document_id,fingerprint], |r| r.get(0)).optional()?))
    }
    pub fn enqueue_document_refresh_observation(
        &self,
        document_id: i64,
        fingerprint: &str,
        timeline_id: Option<i64>,
        now: i64,
    ) -> Result<bool, StorageError> {
        self.with_conn(|conn| {
            let tx=conn.unchecked_transaction()?;
            let inserted=tx.execute(
                "INSERT OR IGNORE INTO bake_document_refresh_observations
             (document_id,fingerprint,source_timeline_id,created_at,updated_at)
             VALUES (?1,?2,?3,?4,?4)",
                params![document_id, fingerprint, timeline_id, now],
            )? > 0;
            if !inserted {
                tx.execute("UPDATE bake_document_refresh_observations SET duplicate_count=duplicate_count+1
                    WHERE document_id=?1 AND fingerprint=?2",params![document_id,fingerprint])?;
            }
            tx.commit()?;
            Ok(inserted)
        })
    }

    pub fn claim_document_refresh_job(
        &self,
        now: i64,
    ) -> Result<Option<DocumentRefreshJob>, StorageError> {
        self.claim_document_refresh_job_with_limit(now, 3)
    }

    pub fn claim_document_refresh_job_with_limit(&self, now: i64, max_attempts: i64)
        -> Result<Option<DocumentRefreshJob>, StorageError> {
        self.claim_document_refresh_job_in_scope(now, max_attempts, None)
    }

    pub fn claim_document_refresh_job_in_scope(&self, now: i64, max_attempts: i64, document_ids: Option<&[i64]>)
        -> Result<Option<DocumentRefreshJob>, StorageError> {
        let scope = document_ids.map(serde_json::to_string).transpose()?;
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            tx.execute("UPDATE bake_document_refresh_attempt_metrics SET outcome='interrupted',finished_at=?1
                WHERE outcome='running' AND lease_id IN (SELECT lease_id FROM bake_document_refresh_observations
                    WHERE state='running' AND lease_until<=?1)",params![now])?;
            tx.execute("UPDATE bake_document_refresh_observations
                SET state=CASE WHEN attempts>=?2 THEN 'blocked' ELSE 'pending' END,
                    last_error='WORKER_INTERRUPTED',lease_id=NULL
                WHERE state='running' AND lease_until<=?1", params![now,max_attempts.clamp(1,5)])?;
            tx.execute("UPDATE bake_document_refresh_observations
                SET state='blocked',last_error=COALESCE(last_error,'RETRY_LIMIT_REACHED'),updated_at=?1
                WHERE state='pending' AND attempts>=?2",params![now,max_attempts.clamp(1,5)])?;
            // The durable lease also prevents multiple worker instances from
            // starting simultaneous background browser reads.
            let running: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM bake_document_refresh_observations WHERE state='running')", [], |r| r.get(0))?;
            if running { tx.commit()?; return Ok(None); }
            let document_id: Option<i64> = tx.query_row(
                "SELECT o.document_id FROM bake_document_refresh_observations o
                 JOIN bake_documents d ON d.id=o.document_id
                 WHERE o.state='pending' AND o.next_attempt_at<=?1 AND d.deleted_at IS NULL
                 AND (?2 IS NULL OR d.id IN (SELECT value FROM json_each(?2)))
                 ORDER BY o.id LIMIT 1", params![now,scope], |r| r.get(0)).optional()?;
            let Some(document_id) = document_id else { tx.commit()?; return Ok(None); };
            let lease_id = uuid::Uuid::new_v4().to_string();
            tx.execute("INSERT INTO bake_document_refresh_attempt_metrics
                (lease_id,document_id,started_at,queue_wait_ms,observation_count,attempt_no)
                SELECT ?2,?1,?3,MAX(0,?3-MIN(MAX(created_at,next_attempt_at))),COUNT(*),MAX(attempts)+1
                FROM bake_document_refresh_observations
                WHERE document_id=?1 AND state='pending' AND next_attempt_at<=?3",
                params![document_id,lease_id,now])?;
            tx.execute("UPDATE bake_document_refresh_observations
                SET state='running',attempts=attempts+1,lease_id=?2,lease_until=?3,updated_at=?4
                WHERE document_id=?1 AND state='pending' AND next_attempt_at<=?4",
                params![document_id,lease_id,now+180_000,now])?;
            let attempts = tx.query_row("SELECT MAX(attempts) FROM bake_document_refresh_observations WHERE lease_id=?1", params![lease_id], |r| r.get(0))?;
            tx.commit()?;
            Ok(Some(DocumentRefreshJob { document_id, lease_id, attempts }))
        })
    }

    pub fn finish_document_refresh_job(
        &self,
        job: &DocumentRefreshJob,
        state: &str,
        error: Option<&str>,
        snapshot_id: Option<i64>,
        now: i64,
        retry_delay: i64,
    ) -> Result<(), StorageError> {
        self.with_conn(|conn| {
            let tx=conn.unchecked_transaction()?;
            tx.execute("UPDATE bake_document_refresh_attempt_metrics
                SET outcome=CASE WHEN ?3 IN ('SOURCE_REFRESH_PAUSED','SOURCE_WRITES_PAUSED') THEN 'paused' ELSE ?2 END,
                    finished_at=?4,execution_wall_ms=CASE WHEN ?3='SOURCE_REFRESH_PAUSED' THEN NULL ELSE MAX(0,?4-started_at) END,
                    scheduled_retry_ms=CASE WHEN ?2='pending' AND COALESCE(?3,'') NOT IN ('SOURCE_REFRESH_PAUSED','SOURCE_WRITES_PAUSED') THEN MAX(0,?5) ELSE 0 END
                WHERE lease_id=?1 AND outcome='running' AND EXISTS(SELECT 1 FROM bake_document_refresh_observations
                    WHERE lease_id=?1 AND state='running')",
                params![job.lease_id,state,error,now,retry_delay])?;
            tx.execute(
                "UPDATE bake_document_refresh_observations SET state=?2,last_error=?3,
                checked_snapshot_id=?4,next_attempt_at=?5,lease_id=NULL,lease_until=0,updated_at=?6
                ,attempts=CASE WHEN ?3 IN ('SOURCE_REFRESH_PAUSED','SOURCE_WRITES_PAUSED') THEN MAX(0,attempts-1) ELSE attempts END
                WHERE lease_id=?1 AND state='running'",
                params![
                    job.lease_id,
                    state,
                    error,
                    snapshot_id,
                    if matches!(error, Some("SOURCE_REFRESH_PAUSED" | "SOURCE_WRITES_PAUSED")) { now } else { now + retry_delay },
                    now
                ],
            )?;
            tx.commit()?;
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn attempt_metrics_count_coalesced_work_once_and_preserve_cancel_terminal_state() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| {c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(1,'test','document',1,1)",[])?;Ok(())}).unwrap();
        assert!(db.enqueue_document_refresh_observation(1,"a",None,100).unwrap());
        assert!(!db.enqueue_document_refresh_observation(1,"a",None,110).unwrap());
        db.enqueue_document_refresh_observation(1,"b",None,120).unwrap();
        let first=db.claim_document_refresh_job(150).unwrap().unwrap();
        db.finish_document_refresh_job(&first,"pending",Some("SCRAPE_TIMEOUT"),None,200,100).unwrap();
        assert!(db.claim_document_refresh_job(250).unwrap().is_none());
        let second=db.claim_document_refresh_job(350).unwrap().unwrap();
        db.cancel_document_refresh_observations(1,400).unwrap();
        db.finish_document_refresh_job(&second,"completed",None,None,500,0).unwrap();
        let metrics=db.document_source_health(0,500).unwrap();
        assert_eq!(metrics["observations"]["duplicate_enqueue_count"],1);
        assert_eq!(metrics["observations"]["total"],2);
        assert_eq!(metrics["attempts"]["claimed"],2);
        assert_eq!(metrics["attempts"]["retry_scheduled"],1);
        assert_eq!(metrics["attempts"]["cancelled"],1);
        assert_eq!(metrics["attempts"]["completed"],0);
        assert_eq!(metrics["attempts"]["scheduled_retry_ms"],100);
        assert_eq!(metrics["attempts"]["mean_queue_wait_ms"],50.0);
        assert_eq!(metrics["attempts"]["mean_execution_wall_ms"],50.0);
        db.with_conn(|c| {
            assert_eq!(c.query_row("SELECT MIN(observation_count) FROM bake_document_refresh_attempt_metrics",[],|r|r.get::<_,i64>(0))?,2);
            Ok(())
        }).unwrap();
    }

    #[test]
    fn interrupted_and_paused_attempts_do_not_invent_execution_durations() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| {c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(1,'test','document',1,1)",[])?;Ok(())}).unwrap();
        db.enqueue_document_refresh_observation(1,"a",None,1).unwrap();
        db.claim_document_refresh_job(2).unwrap().unwrap();
        let next=db.claim_document_refresh_job(180003).unwrap().unwrap();
        db.finish_document_refresh_job(&next,"pending",Some("SOURCE_REFRESH_PAUSED"),None,180004,0).unwrap();
        let metrics=db.document_source_health(0,200000).unwrap();
        assert_eq!(metrics["attempts"]["interrupted"],1);
        assert_eq!(metrics["attempts"]["paused_before_dispatch"],1);
        assert_eq!(metrics["attempts"]["finished_timing_samples"],0);
        assert!(metrics["attempts"]["mean_execution_wall_ms"].is_null());
    }
    #[test]
    fn document_refresh_rollout_selects_eligible_source_without_consuming_other_attempts() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| {
            c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(1,'one','document',1,1),(2,'two','document',1,1)",[])?;
            Ok(())
        }).unwrap();
        db.enqueue_document_refresh_observation(1,"first",None,1).unwrap();
        db.enqueue_document_refresh_observation(2,"second",None,1).unwrap();
        assert!(db.claim_document_refresh_job_in_scope(2,3,Some(&[])).unwrap().is_none());
        let job=db.claim_document_refresh_job_in_scope(2,3,Some(&[2])).unwrap().unwrap();
        assert_eq!(job.document_id,2);
        assert_eq!(db.document_refresh_observation_state(1,"first").unwrap().as_deref(),Some("pending"));
        db.with_conn(|c| {
            assert_eq!(c.query_row("SELECT attempts FROM bake_document_refresh_observations WHERE document_id=1",[],|r|r.get::<_,i64>(0))?,0);
            Ok(())
        }).unwrap();
        db.finish_document_refresh_job(&job,"pending",Some("SOURCE_REFRESH_PAUSED"),None,3,0).unwrap();
        assert_eq!(db.claim_document_refresh_job_in_scope(4,3,None).unwrap().unwrap().document_id,1);
    }

    #[test]
    fn document_refresh_write_pause_keeps_elapsed_time_without_consuming_retry_budget() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| { c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(1,'test','document',1,1)",[])?;Ok(()) }).unwrap();
        db.enqueue_document_refresh_observation(1,"body",None,1).unwrap();
        let job=db.claim_document_refresh_job_with_limit(2,1).unwrap().unwrap();
        db.finish_document_refresh_job(&job,"pending",Some("SOURCE_WRITES_PAUSED"),None,102,30_000).unwrap();
        let health=db.document_source_health(1,102).unwrap();
        assert_eq!(health["attempts"]["paused_after_dispatch"],1);
        assert_eq!(health["attempts"]["paused_before_dispatch"],0);
        assert_eq!(health["attempts"]["mean_execution_wall_ms"],100.0);
        assert_eq!(health["attempts"]["scheduled_retry_ms"],0);
        let next=db.claim_document_refresh_job_with_limit(103,1).unwrap().unwrap();
        assert_eq!(next.attempts,1);
    }

    #[test]
    fn document_refresh_budget_and_pause_do_not_consume_unstarted_attempts() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| { c.execute("INSERT INTO bake_documents(id,title,doc_type,created_at,updated_at) VALUES(1,'test','document',1,1)",[])?;Ok(()) }).unwrap();
        db.enqueue_document_refresh_observation(1,"body",None,1).unwrap();
        let job=db.claim_document_refresh_job_with_limit(2,1).unwrap().unwrap();
        db.finish_document_refresh_job(&job,"pending",Some("SOURCE_REFRESH_PAUSED"),None,3,0).unwrap();
        let retried=db.claim_document_refresh_job_with_limit(4,1).unwrap().unwrap();
        assert_eq!(retried.attempts,1);
        assert!(db.claim_document_refresh_job_with_limit(180_005,1).unwrap().is_none());
        assert_eq!(db.document_refresh_observation_state(1,"body").unwrap().as_deref(),Some("blocked"));
    }
    #[test]
    fn document_refresh_cancel_invalidates_running_and_pending_observations() {
        let db=StorageManager::open_in_memory().unwrap();
        db.with_conn(|c| {c.execute("INSERT INTO bake_documents(title,doc_type,created_at,updated_at) VALUES('test','document',1,1)",[])?;Ok(())}).unwrap();
        db.enqueue_document_refresh_observation(1,"first",Some(1),1).unwrap();
        let job=db.claim_document_refresh_job(2).unwrap().unwrap();
        assert!(db.document_refresh_lease_active(&job.lease_id).unwrap());
        db.enqueue_document_refresh_observation(1,"later",Some(2),3).unwrap();
        assert_eq!(db.cancel_document_refresh_observations(1,4).unwrap(),2);
        assert!(!db.document_refresh_lease_active(&job.lease_id).unwrap());
        db.finish_document_refresh_job(&job,"completed",None,Some(9),5,0).unwrap();
        assert_eq!(db.document_refresh_observation_state(1,"first").unwrap().as_deref(),Some("blocked"));
        assert!(db.claim_document_refresh_job(6).unwrap().is_none());
        db.enqueue_document_refresh_observation(1,"new-visit",Some(3),7).unwrap();
        assert!(db.claim_document_refresh_job(8).unwrap().is_some());
    }
    #[test]
    fn observation_queue_coalesces_without_losing_inflight_updates() {
        let dir = tempfile::tempdir().unwrap();
        let db = StorageManager::open(&dir.path().join("queue.db")).unwrap();
        db.with_conn(|c| { c.execute("INSERT INTO bake_documents(title,doc_type,created_at,updated_at) VALUES('test','document',1,1)", [])?; Ok(()) }).unwrap();
        assert!(db
            .enqueue_document_refresh_observation(1, "a", Some(10), 1)
            .unwrap());
        assert!(!db
            .enqueue_document_refresh_observation(1, "a", Some(10), 2)
            .unwrap());
        db.enqueue_document_refresh_observation(1, "b", Some(11), 2)
            .unwrap();
        assert_eq!(db.document_refresh_status(1).unwrap().unwrap()["state"], "pending");
        let first = db.claim_document_refresh_job(3).unwrap().unwrap();
        assert!(db.claim_document_refresh_job(4).unwrap().is_none());
        db.enqueue_document_refresh_observation(1, "c", Some(12), 4)
            .unwrap();
        assert_eq!(db.document_refresh_status(1).unwrap().unwrap()["state"], "running");
        db.finish_document_refresh_job(&first, "completed", None, None, 5, 0)
            .unwrap();
        // A newer success must not hide an older unfinished observation.
        assert_eq!(db.document_refresh_status(1).unwrap().unwrap()["state"], "pending");
        let next = db.claim_document_refresh_job(6).unwrap().unwrap();
        assert_ne!(first.lease_id, next.lease_id);
        db.finish_document_refresh_job(&first, "blocked", Some("late completion"), None, 7, 0)
            .unwrap();
        assert!(db.claim_document_refresh_job(8).unwrap().is_none());
        let recovered = db.claim_document_refresh_job(180_007).unwrap().unwrap();
        assert_eq!(recovered.attempts, 2);
        db.finish_document_refresh_job(
            &recovered,
            "pending",
            Some("transient"),
            None,
            180_008,
            30_000,
        )
        .unwrap();
        assert!(db.claim_document_refresh_job(180_009).unwrap().is_none());
        assert!(db.claim_document_refresh_job(210_009).unwrap().is_some());
    }
}
