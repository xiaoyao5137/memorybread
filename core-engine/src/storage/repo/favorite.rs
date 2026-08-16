use std::collections::HashSet;

use rusqlite::{params, Connection, OptionalExtension};

use crate::storage::{db::current_ts_ms, error::StorageError, StorageManager};

pub const FAVORITE_KIND_KNOWLEDGE: &str = "knowledge";
pub const FAVORITE_KIND_OPERATION: &str = "operation";
pub const FAVORITE_KIND_DATA: &str = "data";
pub const FAVORITE_KIND_DOCUMENT: &str = "document";

pub fn is_supported_favorite_kind(resource_kind: &str) -> bool {
    matches!(
        resource_kind,
        FAVORITE_KIND_KNOWLEDGE
            | FAVORITE_KIND_OPERATION
            | FAVORITE_KIND_DATA
            | FAVORITE_KIND_DOCUMENT
    )
}

impl StorageManager {
    /// 幂等地收藏或取消收藏一条记忆资源。
    ///
    /// 返回 false 表示目标资源不存在或已删除；收藏表只保存已收藏记录，未收藏是缺省状态。
    pub fn set_memory_favorite(
        &self,
        resource_kind: &str,
        resource_id: i64,
        is_favorite: bool,
    ) -> Result<bool, StorageError> {
        if !is_supported_favorite_kind(resource_kind) || resource_id <= 0 {
            return Ok(false);
        }
        self.with_conn(|conn| {
            if !favorite_resource_exists(conn, resource_kind, resource_id)? {
                return Ok(false);
            }
            if is_favorite {
                let now = current_ts_ms();
                conn.execute(
                    "INSERT INTO memory_favorites (
                        resource_kind, resource_id, created_at, updated_at
                     ) VALUES (?1, ?2, ?3, ?3)
                     ON CONFLICT(resource_kind, resource_id) DO UPDATE
                     SET updated_at = excluded.updated_at",
                    params![resource_kind, resource_id, now],
                )?;
            } else {
                conn.execute(
                    "DELETE FROM memory_favorites
                     WHERE resource_kind = ?1 AND resource_id = ?2",
                    params![resource_kind, resource_id],
                )?;
            }
            Ok(true)
        })
    }

    pub fn is_memory_favorite(
        &self,
        resource_kind: &str,
        resource_id: i64,
    ) -> Result<bool, StorageError> {
        if !is_supported_favorite_kind(resource_kind) || resource_id <= 0 {
            return Ok(false);
        }
        self.with_conn(|conn| {
            conn.query_row(
                "SELECT 1 FROM memory_favorites
                 WHERE resource_kind = ?1 AND resource_id = ?2",
                params![resource_kind, resource_id],
                |_| Ok(true),
            )
            .optional()
            .map(|value| value.unwrap_or(false))
            .map_err(StorageError::Sqlite)
        })
    }

    pub fn list_memory_favorite_ids(
        &self,
        resource_kind: &str,
    ) -> Result<HashSet<i64>, StorageError> {
        if !is_supported_favorite_kind(resource_kind) {
            return Ok(HashSet::new());
        }
        self.with_conn(|conn| {
            let mut stmt =
                conn.prepare("SELECT resource_id FROM memory_favorites WHERE resource_kind = ?1")?;
            let rows = stmt.query_map([resource_kind], |row| row.get::<_, i64>(0))?;
            rows.collect::<Result<HashSet<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    pub(crate) fn delete_memory_favorite_with_conn(
        conn: &Connection,
        resource_kind: &str,
        resource_id: i64,
    ) -> Result<(), StorageError> {
        conn.execute(
            "DELETE FROM memory_favorites
             WHERE resource_kind = ?1 AND resource_id = ?2",
            params![resource_kind, resource_id],
        )?;
        Ok(())
    }
}

fn favorite_resource_exists(
    conn: &Connection,
    resource_kind: &str,
    resource_id: i64,
) -> Result<bool, StorageError> {
    let sql = match resource_kind {
        FAVORITE_KIND_KNOWLEDGE => "SELECT 1 FROM bake_knowledge WHERE id = ?1",
        FAVORITE_KIND_OPERATION => "SELECT 1 FROM bake_sops WHERE id = ?1",
        FAVORITE_KIND_DATA => "SELECT 1 FROM data_sources WHERE id = ?1 AND deleted_at IS NULL",
        FAVORITE_KIND_DOCUMENT => {
            "SELECT 1 FROM bake_documents WHERE id = ?1 AND deleted_at IS NULL"
        }
        _ => return Ok(false),
    };
    conn.query_row(sql, [resource_id], |_| Ok(true))
        .optional()
        .map(|value| value.unwrap_or(false))
        .map_err(StorageError::Sqlite)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::{NewBakeKnowledge, NewBakeSop};

    #[test]
    fn favorite_state_is_idempotent_and_scoped_by_resource_kind() {
        let storage = StorageManager::open_in_memory().unwrap();
        let knowledge_id = storage
            .insert_bake_knowledge(&NewBakeKnowledge {
                timeline_id: 0,
                title: "收藏知识".to_string(),
                summary: "收藏知识".to_string(),
                content: None,
                detailed_content: None,
                entities: "[]".to_string(),
                importance: 5,
                source_capture_ids: Some("[]".to_string()),
            })
            .unwrap();
        let operation_id = storage
            .insert_bake_sop(&NewBakeSop {
                timeline_id: 0,
                title: "收藏操作".to_string(),
                summary: "收藏操作".to_string(),
                content: None,
                detailed_content: None,
                entities: "[]".to_string(),
                importance: 5,
                source_capture_ids: Some("[]".to_string()),
            })
            .unwrap();

        assert!(storage
            .set_memory_favorite(FAVORITE_KIND_KNOWLEDGE, knowledge_id, true)
            .unwrap());
        assert!(storage
            .set_memory_favorite(FAVORITE_KIND_KNOWLEDGE, knowledge_id, true)
            .unwrap());
        assert!(storage
            .is_memory_favorite(FAVORITE_KIND_KNOWLEDGE, knowledge_id)
            .unwrap());
        assert!(!storage
            .is_memory_favorite(FAVORITE_KIND_OPERATION, operation_id)
            .unwrap());
        assert_eq!(
            storage
                .list_memory_favorite_ids(FAVORITE_KIND_KNOWLEDGE)
                .unwrap(),
            HashSet::from([knowledge_id])
        );

        assert!(storage
            .set_memory_favorite(FAVORITE_KIND_KNOWLEDGE, knowledge_id, false)
            .unwrap());
        assert!(!storage
            .is_memory_favorite(FAVORITE_KIND_KNOWLEDGE, knowledge_id)
            .unwrap());

        storage
            .set_memory_favorite(FAVORITE_KIND_KNOWLEDGE, knowledge_id, true)
            .unwrap();
        assert!(storage.delete_bake_knowledge(knowledge_id).unwrap());
        assert!(!storage
            .is_memory_favorite(FAVORITE_KIND_KNOWLEDGE, knowledge_id)
            .unwrap());
    }

    #[test]
    fn favorite_rejects_unknown_or_missing_resources() {
        let storage = StorageManager::open_in_memory().unwrap();
        assert!(!storage.set_memory_favorite("unknown", 1, true).unwrap());
        assert!(!storage
            .set_memory_favorite(FAVORITE_KIND_DOCUMENT, 999, true)
            .unwrap());
    }
}
