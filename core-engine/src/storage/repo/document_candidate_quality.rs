use crate::storage::{error::StorageError, StorageManager};
use rusqlite::params;
use serde_json::json;

#[derive(Clone, Copy)]
pub enum DocumentEvaluationStage {
    Precheck,
    Extraction,
    Persistence,
}

impl DocumentEvaluationStage {
    fn as_str(self) -> &'static str {
        match self {
            Self::Precheck => "precheck",
            Self::Extraction => "extraction",
            Self::Persistence => "persistence",
        }
    }
}

/// Typed input deliberately has no free-form text or model explanation fields.
pub struct DocumentCandidateQualityEvaluation {
    pub run_id: Option<i64>,
    pub timeline_id: i64,
    pub document_id: Option<i64>,
    pub capture_ids: Vec<i64>,
    pub stage: DocumentEvaluationStage,
    pub input_character_count: usize,
    pub has_document_url: bool,
    pub has_document_page_title: bool,
    pub has_substantive_document_body: bool,
    pub allows_auto_create: bool,
}

impl StorageManager {
    pub fn record_document_candidate_quality(
        &self,
        evaluation: &DocumentCandidateQualityEvaluation,
    ) -> Result<(), StorageError> {
        let reason = if evaluation.allows_auto_create {
            "eligible_unverified"
        } else if !evaluation.has_substantive_document_body {
            "body_not_substantive"
        } else {
            "document_identity_evidence_insufficient"
        };
        let evidence = json!({
            "schema_version": "document-candidate-quality.v1",
            "quality_version": crate::services::document_refresh::DOCUMENT_QUALITY_RULE_VERSION,
            "capture_ids": evaluation.capture_ids,
            "capture_id_scope": "candidate_members",
            "snapshot_id": null,
            "input_character_count": evaluation.input_character_count,
            "body_character_count": null, "substantive_block_count": null,
            "excluded_block_count": null, "redaction_ratio": null,
            "coverage": "unverified", "coverage_evidence": null,
            "has_document_url": evaluation.has_document_url,
            "has_document_page_title": evaluation.has_document_page_title,
            "has_substantive_document_body": evaluation.has_substantive_document_body,
            "allows_auto_create": evaluation.allows_auto_create,
            "reason": reason,
        });
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO document_candidate_quality_events
                (run_id,timeline_id,document_id,observed_at,stage,evidence_json)
                VALUES(?1,?2,?3,?4,?5,?6)",
                params![
                    evaluation.run_id,
                    evaluation.timeline_id,
                    evaluation.document_id,
                    chrono::Utc::now().timestamp_millis(),
                    evaluation.stage.as_str(),
                    evidence.to_string()
                ],
            )?;
            Ok(())
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn document_candidate_quality_preserves_unknowns_and_each_stage() {
        let db = StorageManager::open_in_memory().unwrap();
        let mut event = DocumentCandidateQualityEvaluation {
            run_id: Some(7),
            timeline_id: 9,
            document_id: None,
            capture_ids: vec![1, 2],
            stage: DocumentEvaluationStage::Precheck,
            input_character_count: 1000,
            has_document_url: true,
            has_document_page_title: true,
            has_substantive_document_body: false,
            allows_auto_create: false,
        };
        db.record_document_candidate_quality(&event).unwrap();
        event.stage = DocumentEvaluationStage::Persistence;
        event.document_id = Some(10);
        event.has_substantive_document_body = true;
        event.allows_auto_create = true;
        db.record_document_candidate_quality(&event).unwrap();
        let health = db.document_source_health(0, i64::MAX).unwrap();
        assert_eq!(health["candidate_evaluations"][0]["evaluations"], 1);
        assert_eq!(health["candidate_evaluations"][1]["evaluations"], 1);
        db.with_conn(|conn| {
            let mut stmt = conn.prepare("SELECT stage,document_id,evidence_json FROM document_candidate_quality_events ORDER BY id")?;
            let rows = stmt.query_map([], |r| Ok((r.get::<_,String>(0)?,r.get::<_,Option<i64>>(1)?,r.get::<_,String>(2)?)))?
                .collect::<Result<Vec<_>,_>>()?;
            assert_eq!(rows.len(),2);
            assert_eq!(rows[0].0,"precheck");
            assert_eq!(rows[0].1,None);
            assert_eq!(rows[1].1,Some(10));
            let first: serde_json::Value = serde_json::from_str(&rows[0].2).unwrap();
            let second: serde_json::Value = serde_json::from_str(&rows[1].2).unwrap();
            assert_eq!(first["reason"],"body_not_substantive");
            assert_eq!(second["reason"],"eligible_unverified");
            assert_eq!(second["coverage"],"unverified");
            assert_eq!(second["capture_ids"],json!([1,2]));
            for field in ["snapshot_id","body_character_count","substantive_block_count","excluded_block_count","redaction_ratio","coverage_evidence"] {
                assert!(second.get(field).unwrap().is_null(),"{field}");
            }
            assert!(second.get("text").is_none());
            Ok(())
        }).unwrap();
    }
}
