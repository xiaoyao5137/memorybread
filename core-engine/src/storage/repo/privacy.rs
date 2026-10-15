//! 隐私保护模块数据访问层
//!
//! 提供应用黑名单和敏感内容过滤配置的 CRUD 操作

use crate::storage::{
    error::StorageError,
    models::{AppBlacklistRecord, NewAppBlacklist, PrivacyBlockStat, PrivacyFilterRecord},
};
use rusqlite::{params, Connection};
use std::collections::HashSet;

type Result<T> = std::result::Result<T, StorageError>;

fn get_week_start() -> String {
    use chrono::{Datelike, Duration, Local};
    let now = Local::now().date_naive();
    let days_since_monday = now.weekday().num_days_from_monday();
    let monday = now - Duration::days(days_since_monday as i64);
    monday.format("%Y-%m-%d").to_string()
}

// ─────────────────────────────────────────────────────────────────────────────
// 应用黑名单
// ─────────────────────────────────────────────────────────────────────────────

/// 获取所有启用的黑名单 Bundle ID（用于内存缓存）
pub fn get_enabled_blacklist_bundle_ids(conn: &Connection) -> Result<HashSet<String>> {
    let mut stmt = conn.prepare(
        "SELECT bundle_id FROM app_blacklist WHERE enabled = 1 AND bundle_id IS NOT NULL",
    )?;

    let bundle_ids = stmt
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<std::result::Result<HashSet<_>, _>>()?;

    Ok(bundle_ids)
}

/// 获取所有启用的黑名单 Bundle ID 与应用名（用于内存缓存）
pub fn get_enabled_blacklist_entries(conn: &Connection) -> Result<Vec<(Option<String>, String)>> {
    let mut stmt =
        conn.prepare("SELECT bundle_id, app_name FROM app_blacklist WHERE enabled = 1")?;

    let entries = stmt
        .query_map([], |row| {
            Ok((row.get::<_, Option<String>>(0)?, row.get::<_, String>(1)?))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(entries)
}

/// 检查指定 Bundle ID 是否在黑名单中
pub fn is_app_blacklisted(conn: &Connection, bundle_id: &str) -> Result<bool> {
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM app_blacklist WHERE bundle_id = ?1 AND enabled = 1",
        params![bundle_id],
        |row| row.get(0),
    )?;
    Ok(count > 0)
}

/// 获取所有黑名单记录
pub fn list_app_blacklist(conn: &Connection) -> Result<Vec<AppBlacklistRecord>> {
    let mut stmt = conn.prepare(
        "SELECT id, bundle_id, app_name, enabled, reason, created_at, updated_at
         FROM app_blacklist
         ORDER BY created_at DESC",
    )?;

    let records = stmt
        .query_map([], |row| {
            Ok(AppBlacklistRecord {
                id: row.get(0)?,
                bundle_id: row.get(1)?,
                app_name: row.get(2)?,
                enabled: row.get::<_, i64>(3)? == 1,
                reason: row.get(4)?,
                created_at: row.get(5)?,
                updated_at: row.get(6)?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(records)
}

/// 添加应用到黑名单
pub fn add_app_blacklist(conn: &Connection, new: &NewAppBlacklist) -> Result<i64> {
    conn.execute(
        "INSERT INTO app_blacklist (bundle_id, app_name, enabled, reason)
         VALUES (?1, ?2, ?3, ?4)",
        params![
            &new.bundle_id,
            &new.app_name,
            if new.enabled { 1 } else { 0 },
            &new.reason,
        ],
    )?;
    Ok(conn.last_insert_rowid())
}

/// 更新黑名单启用状态
pub fn update_app_blacklist_enabled(conn: &Connection, id: i64, enabled: bool) -> Result<()> {
    let rows = conn.execute(
        "UPDATE app_blacklist SET enabled = ?1, updated_at = datetime('now') WHERE id = ?2",
        params![if enabled { 1 } else { 0 }, id],
    )?;

    if rows == 0 {
        return Err(StorageError::NotFound(format!(
            "黑名单记录 id={} 不存在",
            id
        )));
    }
    Ok(())
}

/// 删除黑名单记录
pub fn delete_app_blacklist(conn: &Connection, id: i64) -> Result<()> {
    let rows = conn.execute("DELETE FROM app_blacklist WHERE id = ?1", params![id])?;
    if rows == 0 {
        return Err(StorageError::NotFound(format!(
            "黑名单记录 id={} 不存在",
            id
        )));
    }
    Ok(())
}

// ─────────────────────────────────────────────────────────────────────────────
// 敏感内容过滤配置
// ─────────────────────────────────────────────────────────────────────────────

/// 获取所有过滤规则
pub fn list_privacy_filters(conn: &Connection) -> Result<Vec<PrivacyFilterRecord>> {
    let mut stmt = conn.prepare(
        "SELECT id, filter_type, filter_name, enabled, config_json, updated_at
         FROM privacy_filters
         ORDER BY id",
    )?;

    let records = stmt
        .query_map([], |row| {
            Ok(PrivacyFilterRecord {
                id: row.get(0)?,
                filter_type: row.get(1)?,
                filter_name: row.get(2)?,
                enabled: row.get::<_, i64>(3)? == 1,
                config_json: row.get(4)?,
                updated_at: row.get(5)?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(records)
}

/// 获取所有启用的过滤规则
pub fn get_enabled_privacy_filters(conn: &Connection) -> Result<Vec<PrivacyFilterRecord>> {
    let mut stmt = conn.prepare(
        "SELECT id, filter_type, filter_name, enabled, config_json, updated_at
         FROM privacy_filters
         WHERE enabled = 1",
    )?;

    let records = stmt
        .query_map([], |row| {
            Ok(PrivacyFilterRecord {
                id: row.get(0)?,
                filter_type: row.get(1)?,
                filter_name: row.get(2)?,
                enabled: row.get::<_, i64>(3)? == 1,
                config_json: row.get(4)?,
                updated_at: row.get(5)?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(records)
}

/// 更新过滤规则启用状态
pub fn update_privacy_filter_enabled(
    conn: &Connection,
    filter_type: &str,
    enabled: bool,
) -> Result<()> {
    let rows = conn.execute(
        "UPDATE privacy_filters SET enabled = ?1, updated_at = datetime('now') WHERE filter_type = ?2",
        params![if enabled { 1 } else { 0 }, filter_type],
    )?;

    if rows == 0 {
        return Err(StorageError::NotFound(format!(
            "过滤规则 {} 不存在",
            filter_type
        )));
    }
    Ok(())
}

/// 更新过滤规则配置
pub fn update_privacy_filter_config(
    conn: &Connection,
    filter_type: &str,
    config_json: &str,
) -> Result<()> {
    let rows = conn.execute(
        "UPDATE privacy_filters SET config_json = ?1, updated_at = datetime('now') WHERE filter_type = ?2",
        params![config_json, filter_type],
    )?;

    if rows == 0 {
        return Err(StorageError::NotFound(format!(
            "过滤规则 {} 不存在",
            filter_type
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn setup_test_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();

        // 创建表
        conn.execute_batch(include_str!(
            "../../../../shared/db-schema/migrations/009_privacy_settings.sql"
        ))
        .unwrap();
        conn.execute_batch(include_str!(
            "../../../../shared/db-schema/migrations/136_app_blacklist_optional_bundle_id.sql"
        ))
        .unwrap();
        conn.execute_batch(include_str!(
            "../../../../shared/db-schema/migrations/059_seed_other_privacy_filter.sql"
        ))
        .unwrap();

        conn
    }

    #[test]
    fn test_blacklist_operations() {
        let conn = setup_test_db();

        // 测试预置数据
        let blacklist = list_app_blacklist(&conn).unwrap();
        assert!(blacklist.len() >= 10);

        // 测试检查黑名单
        assert!(is_app_blacklisted(&conn, "com.tencent.xinWeChat").unwrap());
        assert!(!is_app_blacklisted(&conn, "com.example.unknown").unwrap());

        // 测试获取启用的 Bundle IDs
        let bundle_ids = get_enabled_blacklist_bundle_ids(&conn).unwrap();
        assert!(bundle_ids.contains("com.tencent.xinWeChat"));

        // 测试添加新记录
        let new = NewAppBlacklist {
            bundle_id: Some("com.test.app".to_string()),
            app_name: "测试应用".to_string(),
            enabled: true,
            reason: Some("测试".to_string()),
        };
        let id = add_app_blacklist(&conn, &new).unwrap();
        assert!(id > 0);

        // 测试更新启用状态
        update_app_blacklist_enabled(&conn, id, false).unwrap();
        assert!(!is_app_blacklisted(&conn, "com.test.app").unwrap());

        // 测试删除
        delete_app_blacklist(&conn, id).unwrap();
        let blacklist = list_app_blacklist(&conn).unwrap();
        assert!(!blacklist.iter().any(|r| r.id == id));
    }

    #[test]
    fn test_name_only_blacklist_rows_and_legacy_migration() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(include_str!(
            "../../../../shared/db-schema/migrations/009_privacy_settings.sql"
        ))
        .unwrap();
        conn.execute(
            "INSERT INTO app_blacklist (bundle_id, app_name, enabled, reason, created_at, updated_at)
             VALUES ('', '旧应用', 0, '旧备注', '2024-01-01', '2024-02-01')",
            [],
        )
        .unwrap();
        let legacy_id = conn.last_insert_rowid();
        let mut whitespace_ids = Vec::new();
        for bundle_id in [" ", "  "] {
            conn.execute(
                "INSERT INTO app_blacklist (bundle_id, app_name, enabled, reason)
                 VALUES (?1, '同名旧应用', 1, '保留不同旧记录')",
                params![bundle_id],
            )
            .unwrap();
            whitespace_ids.push(conn.last_insert_rowid());
        }
        conn.execute_batch(include_str!(
            "../../../../shared/db-schema/migrations/136_app_blacklist_optional_bundle_id.sql"
        ))
        .unwrap();

        let legacy = list_app_blacklist(&conn)
            .unwrap()
            .into_iter()
            .find(|row| row.id == legacy_id)
            .unwrap();
        assert_eq!(legacy.bundle_id, None);
        assert_eq!(legacy.app_name, "旧应用");
        assert!(!legacy.enabled);
        assert_eq!(legacy.reason.as_deref(), Some("旧备注"));
        assert_eq!(legacy.created_at, "2024-01-01");
        assert_eq!(legacy.updated_at, "2024-02-01");
        let migrated = list_app_blacklist(&conn).unwrap();
        for (id, bundle_id) in whitespace_ids.iter().zip([" ", "  "]) {
            let row = migrated.iter().find(|row| row.id == *id).unwrap();
            assert_eq!(row.bundle_id.as_deref(), Some(bundle_id));
            assert_eq!(row.app_name, "同名旧应用");
        }

        for name in ["钉钉", "企业微信"] {
            add_app_blacklist(
                &conn,
                &NewAppBlacklist {
                    bundle_id: None,
                    app_name: name.to_string(),
                    enabled: true,
                    reason: None,
                },
            )
            .unwrap();
        }
        let entries = get_enabled_blacklist_entries(&conn).unwrap();
        assert!(entries.contains(&(None, "钉钉".to_string())));
        assert!(entries.contains(&(None, "企业微信".to_string())));
        assert!(add_app_blacklist(
            &conn,
            &NewAppBlacklist {
                bundle_id: None,
                app_name: "钉钉".to_string(),
                enabled: true,
                reason: None,
            }
        )
        .is_err());
    }

    #[test]
    fn test_privacy_filter_operations() {
        let conn = setup_test_db();

        // 测试预置数据
        let filters = list_privacy_filters(&conn).unwrap();
        assert_eq!(filters.len(), 4);
        let other_filter = filters
            .iter()
            .find(|filter| filter.filter_type == "other")
            .unwrap();
        assert_eq!(other_filter.filter_name, "其它敏感信息过滤");
        assert!(other_filter
            .config_json
            .as_deref()
            .is_some_and(|config| config.contains("色情信息")));

        // 测试获取启用的过滤规则
        let enabled = get_enabled_privacy_filters(&conn).unwrap();
        assert_eq!(enabled.len(), 4);

        // 测试更新启用状态
        update_privacy_filter_enabled(&conn, "chat", false).unwrap();
        let enabled = get_enabled_privacy_filters(&conn).unwrap();
        assert_eq!(enabled.len(), 3);

        // 测试更新配置
        let new_config = r#"{"test": "value"}"#;
        update_privacy_filter_config(&conn, "pii", new_config).unwrap();

        let filters = list_privacy_filters(&conn).unwrap();
        let pii_filter = filters.iter().find(|f| f.filter_type == "pii").unwrap();
        assert_eq!(pii_filter.config_json.as_deref(), Some(new_config));
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 隐私拦截统计
// ─────────────────────────────────────────────────────────────────────────────

/// 增加拦截计数
pub fn increment_block_stat(conn: &Connection, stat_type: &str, target_id: &str) -> Result<()> {
    let week_start = get_week_start();
    conn.execute(
        "INSERT INTO privacy_block_stats (stat_type, target_id, block_count, week_start)
         VALUES (?1, ?2, 1, ?3)
         ON CONFLICT(stat_type, target_id, week_start)
         DO UPDATE SET block_count = block_count + 1, updated_at = datetime('now')",
        params![stat_type, target_id, week_start],
    )?;
    Ok(())
}

/// 获取本周拦截统计
pub fn get_week_block_stats(conn: &Connection) -> Result<Vec<PrivacyBlockStat>> {
    let week_start = get_week_start();
    let mut stmt = conn.prepare(
        "SELECT id, stat_type, target_id, block_count, week_start, updated_at
         FROM privacy_block_stats
         WHERE week_start = ?1",
    )?;
    let rows = stmt.query_map(params![week_start], |row| {
        Ok(PrivacyBlockStat {
            id: row.get(0)?,
            stat_type: row.get(1)?,
            target_id: row.get(2)?,
            block_count: row.get(3)?,
            week_start: row.get(4)?,
            updated_at: row.get(5)?,
        })
    })?;
    rows.collect::<std::result::Result<Vec<_>, _>>()
        .map_err(StorageError::from)
}
