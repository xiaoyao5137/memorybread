//! 客户端本地面包屑规则、结算、库存与佩戴状态。

use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::storage::{db::current_ts_ms, error::StorageError, StorageManager};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BreadcrumbDefinition {
    pub id: String,
    pub breadcrumb_key: String,
    pub name: String,
    pub tagline: String,
    pub description: String,
    pub icon_key: String,
    pub palette_key: String,
    pub rarity: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BreadcrumbCalculationRule {
    pub period: String,
    pub metric_key: String,
    pub threshold: String,
    pub metric_unit: String,
    pub increment: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BreadcrumbRule {
    pub id: String,
    pub rule_key: String,
    pub title: String,
    pub description: String,
    pub breadcrumb: BreadcrumbDefinition,
    pub calculation: BreadcrumbCalculationRule,
    pub starts_at: Option<String>,
    pub expires_at: Option<String>,
    pub version: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OwnedBreadcrumb {
    pub breadcrumb: BreadcrumbDefinition,
    pub quantity: i32,
    pub first_earned_at: i64,
    pub last_earned_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct EquippedBreadcrumbs {
    pub profile_avatar: Option<BreadcrumbDefinition>,
    pub floating_avatar: Option<BreadcrumbDefinition>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default, PartialEq)]
pub struct BreadcrumbProfile {
    pub breadcrumbs: Vec<OwnedBreadcrumb>,
    pub equipped: EquippedBreadcrumbs,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BreadcrumbAwardResult {
    pub awarded: bool,
    pub breadcrumb: BreadcrumbDefinition,
    pub increment: i32,
    pub total_quantity: i32,
}

impl StorageManager {
    pub fn sync_breadcrumb_rules(&self, rules: &[BreadcrumbRule]) -> Result<(), StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            // 一次成功的服务端同步代表完整的当前规则集。保留历史规则以维持本地
            // 发放记录的外键，但立即停用服务端已撤下的规则。
            tx.execute("UPDATE breadcrumb_rules SET is_active = 0", [])?;
            for rule in rules {
                let breadcrumb = &rule.breadcrumb;
                tx.execute(
                    "INSERT INTO breadcrumb_definitions (
                       id, breadcrumb_key, name, tagline, description, icon_key, palette_key,
                       rarity, updated_at
                     ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
                     ON CONFLICT(id) DO UPDATE SET
                       breadcrumb_key = excluded.breadcrumb_key,
                       name = excluded.name,
                       tagline = excluded.tagline,
                       description = excluded.description,
                       icon_key = excluded.icon_key,
                       palette_key = excluded.palette_key,
                       rarity = excluded.rarity,
                       updated_at = excluded.updated_at",
                    params![
                        breadcrumb.id,
                        breadcrumb.breadcrumb_key,
                        breadcrumb.name,
                        breadcrumb.tagline,
                        breadcrumb.description,
                        breadcrumb.icon_key,
                        breadcrumb.palette_key,
                        breadcrumb.rarity,
                        now,
                    ],
                )?;
                tx.execute(
                    "INSERT INTO breadcrumb_rules (
                       id, rule_key, breadcrumb_id, title, description, period, metric_key,
                       threshold, metric_unit, increment, version, starts_at, expires_at,
                       is_active, updated_at
                     ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, 1, ?14)
                     ON CONFLICT(id) DO UPDATE SET
                       rule_key = excluded.rule_key,
                       breadcrumb_id = excluded.breadcrumb_id,
                       title = excluded.title,
                       description = excluded.description,
                       period = excluded.period,
                       metric_key = excluded.metric_key,
                       threshold = excluded.threshold,
                       metric_unit = excluded.metric_unit,
                       increment = excluded.increment,
                       version = excluded.version,
                       starts_at = excluded.starts_at,
                       expires_at = excluded.expires_at,
                       is_active = 1,
                       updated_at = excluded.updated_at",
                    params![
                        rule.id,
                        rule.rule_key,
                        breadcrumb.id,
                        rule.title,
                        rule.description,
                        rule.calculation.period,
                        rule.calculation.metric_key,
                        rule.calculation.threshold,
                        rule.calculation.metric_unit,
                        rule.calculation.increment,
                        rule.version,
                        rule.starts_at,
                        rule.expires_at,
                        now,
                    ],
                )?;
            }
            tx.commit()?;
            Ok(())
        })
    }

    pub fn award_breadcrumb(
        &self,
        rule_id: &str,
        period_key: &str,
        observed_value: f64,
    ) -> Result<BreadcrumbAwardResult, StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            let tx = conn.unchecked_transaction()?;
            let (breadcrumb_id, threshold, increment, rule_version): (String, String, i32, i32) =
                tx.query_row(
                    "SELECT breadcrumb_id, threshold, increment, version
                     FROM breadcrumb_rules WHERE id = ?1 AND is_active = 1",
                    params![rule_id],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
                )
                .optional()?
                .ok_or_else(|| StorageError::NotFound(format!("breadcrumb rule '{rule_id}'")))?;
            let parsed_threshold =
                threshold
                    .parse::<f64>()
                    .map_err(|_| StorageError::MigrationFailed {
                        version: "breadcrumb_rule",
                        reason: format!("invalid threshold for rule '{rule_id}'"),
                    })?;
            if !observed_value.is_finite() || observed_value < parsed_threshold {
                return Err(StorageError::NotFound(format!(
                    "breadcrumb threshold not met for rule '{rule_id}'"
                )));
            }

            let inserted = tx.execute(
                "INSERT OR IGNORE INTO breadcrumb_awards (
                   rule_id, period_key, breadcrumb_id, observed_value, increment,
                   rule_version, awarded_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
                params![
                    rule_id,
                    period_key,
                    breadcrumb_id,
                    observed_value.to_string(),
                    increment,
                    rule_version,
                    now,
                ],
            )? > 0;

            if inserted {
                tx.execute(
                    "INSERT INTO breadcrumb_inventory (
                       breadcrumb_id, quantity, first_earned_at, last_earned_at, updated_at
                     ) VALUES (?1, ?2, ?3, ?3, ?3)
                     ON CONFLICT(breadcrumb_id) DO UPDATE SET
                       quantity = breadcrumb_inventory.quantity + excluded.quantity,
                       last_earned_at = excluded.last_earned_at,
                       updated_at = excluded.updated_at",
                    params![breadcrumb_id, increment, now],
                )?;
            }

            let total_quantity = tx.query_row(
                "SELECT quantity FROM breadcrumb_inventory WHERE breadcrumb_id = ?1",
                params![breadcrumb_id],
                |row| row.get(0),
            )?;
            let breadcrumb = load_breadcrumb(&tx, &breadcrumb_id)?;
            tx.commit()?;
            Ok(BreadcrumbAwardResult {
                awarded: inserted,
                breadcrumb,
                increment: if inserted { increment } else { 0 },
                total_quantity,
            })
        })
    }

    pub fn list_breadcrumb_profile(&self) -> Result<BreadcrumbProfile, StorageError> {
        self.with_conn(load_profile)
    }

    pub fn list_breadcrumb_rules(&self) -> Result<Vec<BreadcrumbRule>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(
                "SELECT r.id, r.rule_key, r.title, r.description, r.period, r.metric_key,
                        r.threshold, r.metric_unit, r.increment, r.version, r.starts_at,
                        r.expires_at, b.id, b.breadcrumb_key, b.name, b.tagline,
                        b.description, b.icon_key, b.palette_key, b.rarity
                 FROM breadcrumb_rules r
                 JOIN breadcrumb_definitions b ON b.id = r.breadcrumb_id
                 WHERE r.is_active = 1
                 ORDER BY r.updated_at DESC, r.id",
            )?;
            let rows = stmt.query_map([], |row| {
                Ok(BreadcrumbRule {
                    id: row.get(0)?,
                    rule_key: row.get(1)?,
                    title: row.get(2)?,
                    description: row.get(3)?,
                    calculation: BreadcrumbCalculationRule {
                        period: row.get(4)?,
                        metric_key: row.get(5)?,
                        threshold: row.get(6)?,
                        metric_unit: row.get(7)?,
                        increment: row.get(8)?,
                    },
                    version: row.get(9)?,
                    starts_at: row.get(10)?,
                    expires_at: row.get(11)?,
                    breadcrumb: BreadcrumbDefinition {
                        id: row.get(12)?,
                        breadcrumb_key: row.get(13)?,
                        name: row.get(14)?,
                        tagline: row.get(15)?,
                        description: row.get(16)?,
                        icon_key: row.get(17)?,
                        palette_key: row.get(18)?,
                        rarity: row.get(19)?,
                    },
                })
            })?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    pub fn equip_breadcrumb(
        &self,
        surface: &str,
        breadcrumb_id: Option<&str>,
    ) -> Result<BreadcrumbProfile, StorageError> {
        if !matches!(surface, "profile_avatar" | "floating_avatar") {
            return Err(StorageError::NotFound(format!(
                "unsupported breadcrumb surface '{surface}'"
            )));
        }
        self.with_conn(|conn| {
            if let Some(breadcrumb_id) = breadcrumb_id {
                let owned: bool = conn.query_row(
                    "SELECT EXISTS(
                       SELECT 1 FROM breadcrumb_inventory
                       WHERE breadcrumb_id = ?1 AND quantity > 0
                     )",
                    params![breadcrumb_id],
                    |row| row.get(0),
                )?;
                if !owned {
                    return Err(StorageError::NotFound(format!(
                        "breadcrumb '{breadcrumb_id}' is not owned"
                    )));
                }
                conn.execute(
                    "INSERT INTO breadcrumb_equipment (surface, breadcrumb_id, equipped_at)
                     VALUES (?1, ?2, ?3)
                     ON CONFLICT(surface) DO UPDATE SET
                       breadcrumb_id = excluded.breadcrumb_id,
                       equipped_at = excluded.equipped_at",
                    params![surface, breadcrumb_id, current_ts_ms()],
                )?;
            } else {
                conn.execute(
                    "DELETE FROM breadcrumb_equipment WHERE surface = ?1",
                    params![surface],
                )?;
            }
            load_profile(conn)
        })
    }
}

fn load_breadcrumb(
    conn: &rusqlite::Connection,
    breadcrumb_id: &str,
) -> Result<BreadcrumbDefinition, StorageError> {
    conn.query_row(
        "SELECT id, breadcrumb_key, name, tagline, description, icon_key, palette_key, rarity
         FROM breadcrumb_definitions WHERE id = ?1",
        params![breadcrumb_id],
        row_to_breadcrumb,
    )
    .map_err(StorageError::Sqlite)
}

fn row_to_breadcrumb(row: &rusqlite::Row<'_>) -> rusqlite::Result<BreadcrumbDefinition> {
    Ok(BreadcrumbDefinition {
        id: row.get(0)?,
        breadcrumb_key: row.get(1)?,
        name: row.get(2)?,
        tagline: row.get(3)?,
        description: row.get(4)?,
        icon_key: row.get(5)?,
        palette_key: row.get(6)?,
        rarity: row.get(7)?,
    })
}

fn load_profile(conn: &rusqlite::Connection) -> Result<BreadcrumbProfile, StorageError> {
    let mut stmt = conn.prepare(
        "SELECT b.id, b.breadcrumb_key, b.name, b.tagline, b.description, b.icon_key,
                b.palette_key, b.rarity, i.quantity, i.first_earned_at, i.last_earned_at
         FROM breadcrumb_inventory i
         JOIN breadcrumb_definitions b ON b.id = i.breadcrumb_id
         WHERE i.quantity > 0
         ORDER BY i.last_earned_at DESC, b.breadcrumb_key",
    )?;
    let breadcrumbs = stmt
        .query_map([], |row| {
            Ok(OwnedBreadcrumb {
                breadcrumb: row_to_breadcrumb(row)?,
                quantity: row.get(8)?,
                first_earned_at: row.get(9)?,
                last_earned_at: row.get(10)?,
            })
        })?
        .collect::<Result<Vec<_>, _>>()?;

    let mut equipped = EquippedBreadcrumbs::default();
    let mut equipment_stmt = conn.prepare(
        "SELECT e.surface, b.id, b.breadcrumb_key, b.name, b.tagline, b.description,
                b.icon_key, b.palette_key, b.rarity
         FROM breadcrumb_equipment e
         JOIN breadcrumb_definitions b ON b.id = e.breadcrumb_id",
    )?;
    let rows = equipment_stmt.query_map([], |row| {
        let surface: String = row.get(0)?;
        let breadcrumb = BreadcrumbDefinition {
            id: row.get(1)?,
            breadcrumb_key: row.get(2)?,
            name: row.get(3)?,
            tagline: row.get(4)?,
            description: row.get(5)?,
            icon_key: row.get(6)?,
            palette_key: row.get(7)?,
            rarity: row.get(8)?,
        };
        Ok((surface, breadcrumb))
    })?;
    for row in rows {
        let (surface, breadcrumb) = row?;
        match surface.as_str() {
            "profile_avatar" => equipped.profile_avatar = Some(breadcrumb),
            "floating_avatar" => equipped.floating_avatar = Some(breadcrumb),
            _ => {}
        }
    }

    Ok(BreadcrumbProfile {
        breadcrumbs,
        equipped,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule() -> BreadcrumbRule {
        BreadcrumbRule {
            id: "rule-1".to_string(),
            rule_key: "weekly_focus".to_string(),
            title: "本周专注".to_string(),
            description: "本周专注达到一百分钟。".to_string(),
            breadcrumb: BreadcrumbDefinition {
                id: "breadcrumb-1".to_string(),
                breadcrumb_key: "focused".to_string(),
                name: "专注面包屑".to_string(),
                tagline: "专注留下痕迹".to_string(),
                description: "记录稳定的专注投入。".to_string(),
                icon_key: "focus".to_string(),
                palette_key: "forest".to_string(),
                rarity: "common".to_string(),
            },
            calculation: BreadcrumbCalculationRule {
                period: "weekly".to_string(),
                metric_key: "focus_minutes".to_string(),
                threshold: "100".to_string(),
                metric_unit: "minute".to_string(),
                increment: 1,
            },
            starts_at: None,
            expires_at: None,
            version: 1,
        }
    }

    #[test]
    fn award_is_local_and_idempotent_per_rule_period() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage.sync_breadcrumb_rules(&[rule()]).unwrap();

        let first = storage
            .award_breadcrumb("rule-1", "2026-W33", 120.0)
            .unwrap();
        let second = storage
            .award_breadcrumb("rule-1", "2026-W33", 150.0)
            .unwrap();

        assert!(first.awarded);
        assert!(!second.awarded);
        assert_eq!(second.total_quantity, 1);
        assert_eq!(
            storage.list_breadcrumb_profile().unwrap().breadcrumbs.len(),
            1
        );
        assert_eq!(storage.list_breadcrumb_rules().unwrap(), vec![rule()]);
    }

    #[test]
    fn successful_sync_deactivates_rules_removed_by_server_without_losing_inventory() {
        let storage = StorageManager::open_in_memory().unwrap();
        storage.sync_breadcrumb_rules(&[rule()]).unwrap();
        storage
            .award_breadcrumb("rule-1", "2026-W33", 120.0)
            .unwrap();

        storage.sync_breadcrumb_rules(&[]).unwrap();

        assert!(storage.list_breadcrumb_rules().unwrap().is_empty());
        assert!(storage
            .award_breadcrumb("rule-1", "2026-W34", 120.0)
            .is_err());
        assert_eq!(
            storage.list_breadcrumb_profile().unwrap().breadcrumbs.len(),
            1
        );
    }
}
