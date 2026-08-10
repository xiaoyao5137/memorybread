use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct IntegrationSkillLogEntry {
    pub ts: i64,
    pub level: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct IntegrationSkillRunRecord {
    pub id: String,
    pub skill_id: String,
    pub mode: String,
    pub status: String,
    pub input_summary: Value,
    pub result: Option<Value>,
    pub logs: Vec<IntegrationSkillLogEntry>,
    pub error_code: Option<String>,
    pub error_message: Option<String>,
    pub created_at_ms: i64,
    pub started_at_ms: Option<i64>,
    pub finished_at_ms: Option<i64>,
}

#[derive(Debug, Clone)]
pub struct ImportedKnowledgeItem {
    pub source_key: String,
    pub source_path: String,
    pub title: String,
    pub content: String,
    pub entities: Vec<String>,
    pub metadata: Value,
    pub content_hash: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImportWriteOutcome {
    Created(i64),
    Updated(i64),
    Unchanged(i64),
}
