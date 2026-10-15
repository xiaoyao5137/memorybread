/// 应用黑名单检测模块
///
/// 提供快速的应用黑名单检测，支持内存缓存和定期刷新
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};
use tracing::debug;

use crate::storage::repo::privacy;
use crate::storage::StorageManager;

/// 黑名单缓存刷新间隔（30 秒）
const CACHE_REFRESH_INTERVAL: Duration = Duration::from_secs(30);

/// 应用黑名单检测器
#[derive(Clone)]
pub struct BlacklistChecker {
    storage: StorageManager,
    cache: Arc<RwLock<BlacklistCache>>,
}

struct BlacklistCache {
    bundle_ids: HashSet<String>,
    app_names: HashMap<String, String>,
    last_refresh: Instant,
    seen_revision: u64,
}

impl BlacklistChecker {
    /// 创建新的黑名单检测器
    pub fn new(storage: StorageManager) -> Self {
        let checker = Self {
            storage,
            cache: Arc::new(RwLock::new(BlacklistCache {
                bundle_ids: HashSet::new(),
                app_names: HashMap::new(),
                last_refresh: Instant::now() - CACHE_REFRESH_INTERVAL, // 强制首次加载
                seen_revision: 0,
            })),
        };

        // 立即加载一次
        if let Err(e) = checker.refresh_cache() {
            tracing::warn!("初始化黑名单缓存失败: {}", e);
        }

        checker
    }

    /// 检查指定 Bundle ID 是否在黑名单中
    pub fn is_blacklisted(&self, bundle_id: &str) -> bool {
        self.is_blacklisted_by_bundle_or_name(Some(bundle_id), None)
    }

    /// 检查指定 Bundle ID 或应用名是否在黑名单中
    pub fn is_blacklisted_by_bundle_or_name(
        &self,
        bundle_id: Option<&str>,
        app_name: Option<&str>,
    ) -> bool {
        self.matched_target(bundle_id, app_name).is_some()
    }

    /// Return the configured identifier that matched, so block statistics are
    /// attributed to a name-only rule even when the observed app has a Bundle ID.
    pub fn matched_target(
        &self,
        bundle_id: Option<&str>,
        app_name: Option<&str>,
    ) -> Option<String> {
        // 检查是否需要刷新缓存
        {
            let cache = self.cache.read().unwrap();
            if cache.last_refresh.elapsed() > CACHE_REFRESH_INTERVAL
                || cache.seen_revision != self.storage.blacklist_revision()
            {
                drop(cache); // 释放读锁
                if let Err(e) = self.refresh_cache() {
                    tracing::warn!("刷新黑名单缓存失败: {}", e);
                }
            }
        }

        // 查询缓存
        let cache = self.cache.read().unwrap();
        if let Some(id) = bundle_id.filter(|id| cache.bundle_ids.contains(*id)) {
            return Some(id.to_string());
        }
        app_name
            .map(normalize_app_name)
            .and_then(|name| cache.app_names.get(&name).cloned())
    }

    /// 从数据库刷新缓存
    fn refresh_cache(&self) -> Result<(), Box<dyn std::error::Error>> {
        let revision = self.storage.blacklist_revision();
        let entries = self
            .storage
            .with_conn(|conn| privacy::get_enabled_blacklist_entries(conn))?;

        let mut cache = self.cache.write().unwrap();
        cache.bundle_ids = entries
            .iter()
            .filter_map(|(bundle_id, _)| bundle_id.clone())
            .collect();
        let mut names = HashMap::new();
        for (bundle_id, app_name) in entries {
            let normalized = normalize_app_name(&app_name);
            if normalized.is_empty() {
                continue;
            }
            if bundle_id.is_none() {
                names.insert(normalized, app_name);
            } else {
                names.entry(normalized).or_insert(app_name);
            }
        }
        cache.app_names = names;
        cache.last_refresh = Instant::now();
        cache.seen_revision = revision;

        debug!(
            "黑名单缓存已刷新，共 {} 个 Bundle ID、{} 个应用名",
            cache.bundle_ids.len(),
            cache.app_names.len()
        );
        Ok(())
    }

    /// 手动刷新缓存（用于配置更新后立即生效）
    pub fn force_refresh(&self) -> Result<(), Box<dyn std::error::Error>> {
        self.refresh_cache()
    }
}

fn normalize_app_name(name: &str) -> String {
    name.trim().to_lowercase()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::{models::NewAppBlacklist, repo::privacy, StorageManager};

    #[test]
    fn test_blacklist_checker() {
        let storage = StorageManager::open_in_memory().unwrap();

        let checker = BlacklistChecker::new(storage);

        // 测试预置黑名单
        assert!(checker.is_blacklisted("com.tencent.xinWeChat"));
        assert!(checker.is_blacklisted("com.tencent.qq"));
        assert!(!checker.is_blacklisted("com.google.Chrome"));
    }

    #[test]
    fn name_only_rule_matches_observed_bundle_and_updates_without_timeout() {
        let storage = StorageManager::open_in_memory().unwrap();
        let checker = BlacklistChecker::new(storage.clone());
        let bundle = Some("com.alibaba.DingTalkMac");
        assert_eq!(checker.matched_target(bundle, Some("钉钉")), None);

        let id = storage
            .with_conn(|conn| {
                privacy::add_app_blacklist(
                    conn,
                    &NewAppBlacklist {
                        bundle_id: None,
                        app_name: "钉钉".to_string(),
                        enabled: true,
                        reason: None,
                    },
                )
            })
            .unwrap();
        storage.notify_blacklist_changed();
        assert_eq!(
            checker.matched_target(bundle, Some("  钉钉  ")),
            Some("钉钉".to_string())
        );
        assert_eq!(checker.matched_target(bundle, Some("钉钉测试")), None);

        storage
            .with_conn(|conn| privacy::update_app_blacklist_enabled(conn, id, false))
            .unwrap();
        storage.notify_blacklist_changed();
        assert_eq!(checker.matched_target(bundle, Some("钉钉")), None);

        storage
            .with_conn(|conn| privacy::update_app_blacklist_enabled(conn, id, true))
            .unwrap();
        storage.notify_blacklist_changed();
        assert_eq!(
            checker.matched_target(bundle, Some("钉钉")),
            Some("钉钉".into())
        );

        storage
            .with_conn(|conn| privacy::delete_app_blacklist(conn, id))
            .unwrap();
        storage.notify_blacklist_changed();
        assert_eq!(checker.matched_target(bundle, Some("钉钉")), None);
    }
}
