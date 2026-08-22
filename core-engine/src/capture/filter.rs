//! 隐私过滤器
//!
//! 判断当前屏幕内容是否属于敏感场景（密码框、黑名单应用等），
//! 如果是则跳过详细采集，仅记录 is_sensitive=1 的占位行。

use std::collections::HashSet;

// ─────────────────────────────────────────────────────────────────────────────
// 内置默认黑名单
// ─────────────────────────────────────────────────────────────────────────────

const DEFAULT_BLOCKED_APPS: &[&str] = &[
    "System Preferences",
    "System Settings",
    "memory-bread",
    "memory-bread-desktop",
    "记忆面包",
];

const DEFAULT_BLOCKED_BUNDLE_IDS: &[&str] = &["com.apple.systempreferences"];

/// 窗口标题中出现这些关键词时判定为敏感
const SENSITIVE_WIN_KEYWORDS: &[&str] = &[
    "密码",
    "password",
    "Password",
    "PIN",
    "私钥",
    "secret",
    "Secret",
    "passphrase",
    "Passphrase",
    "memory-bread",
    "MemoryBread Preview",
    "记忆面包",
    "KnowledgePanel",
    "MonitorPanel",
    "RagPanel",
];

/// 被判定为密码输入框的 AX Role
const PASSWORD_AX_ROLES: &[&str] = &["AXSecureTextField"];

// ─────────────────────────────────────────────────────────────────────────────
// PrivacyFilter
// ─────────────────────────────────────────────────────────────────────────────

/// 隐私过滤器，判断当前场景是否应跳过详细采集。
#[derive(Debug, Clone)]
pub struct PrivacyFilter {
    blocked_apps: HashSet<String>,
    blocked_bundle_ids: HashSet<String>,
}

impl Default for PrivacyFilter {
    fn default() -> Self {
        Self {
            blocked_apps: DEFAULT_BLOCKED_APPS.iter().map(|s| s.to_string()).collect(),
            blocked_bundle_ids: DEFAULT_BLOCKED_BUNDLE_IDS
                .iter()
                .map(|s| s.to_string())
                .collect(),
        }
    }
}

impl PrivacyFilter {
    /// 使用内置默认黑名单创建过滤器。
    pub fn new() -> Self {
        Self::default()
    }

    /// 追加额外的黑名单应用（通常从数据库 app_filters 表加载）。
    pub fn with_extra_blocked_apps(mut self, apps: &[String]) -> Self {
        for app in apps {
            self.blocked_apps.insert(app.clone());
        }
        self
    }

    /// 追加额外的 Bundle ID 黑名单。
    pub fn with_extra_blocked_bundle_ids(mut self, ids: &[String]) -> Self {
        for id in ids {
            self.blocked_bundle_ids.insert(id.clone());
        }
        self
    }

    /// 判断给定场景是否敏感。
    ///
    /// 任意一条规则命中即返回 `true`（应跳过详细采集）。
    pub fn is_sensitive(
        &self,
        app_name: Option<&str>,
        bundle_id: Option<&str>,
        ax_role: Option<&str>,
        win_title: Option<&str>,
    ) -> bool {
        // 1. 应用名黑名单
        if let Some(app) = app_name {
            if self.blocked_apps.contains(app) {
                return true;
            }
        }

        // 2. Bundle ID 黑名单
        if let Some(bid) = bundle_id {
            if self.blocked_bundle_ids.contains(bid) {
                return true;
            }
        }

        // 3. AX 角色（密码输入框）
        if let Some(role) = ax_role {
            if PASSWORD_AX_ROLES.contains(&role) {
                return true;
            }
        }

        // 4. 窗口标题关键词（不区分大小写）
        if let Some(title) = win_title {
            let title_lower = title.to_lowercase();
            for kw in SENSITIVE_WIN_KEYWORDS {
                if title_lower.contains(&kw.to_lowercase()) {
                    return true;
                }
            }
        }

        false
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// 测试
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn f() -> PrivacyFilter {
        PrivacyFilter::new()
    }

    #[test]
    fn test_blocked_app_name() {
        assert!(f().is_sensitive(Some("System Settings"), None, None, None));
        assert!(f().is_sensitive(Some("memory-bread"), None, None, None));
        assert!(!f().is_sensitive(Some("Feishu"), None, None, None));
    }

    #[test]
    fn test_blocked_bundle_id() {
        assert!(f().is_sensitive(None, Some("com.apple.systempreferences"), None, None));
        assert!(!f().is_sensitive(None, Some("com.tencent.xinWeChat"), None, None));
        assert!(!f().is_sensitive(None, Some("com.apple.Photos"), None, None));
        assert!(!f().is_sensitive(None, Some("com.google.Chrome"), None, None));
    }

    #[test]
    fn test_password_ax_role() {
        assert!(f().is_sensitive(None, None, Some("AXSecureTextField"), None));
        assert!(!f().is_sensitive(None, None, Some("AXTextField"), None));
        assert!(!f().is_sensitive(None, None, Some("AXButton"), None));
    }

    #[test]
    fn test_sensitive_win_title() {
        assert!(f().is_sensitive(None, None, None, Some("Enter Password")));
        assert!(f().is_sensitive(None, None, None, Some("输入密码")));
        assert!(f().is_sensitive(None, None, None, Some("SSH Passphrase")));
        assert!(!f().is_sensitive(None, None, None, Some("Work Document")));
        assert!(!f().is_sensitive(None, None, None, Some("Feishu - 工作群")));
    }

    #[test]
    fn test_case_insensitive_title() {
        assert!(f().is_sensitive(None, None, None, Some("ENTER PASSWORD")));
    }

    #[test]
    fn test_memory_bread_app_is_sensitive() {
        assert!(f().is_sensitive(Some("memory-bread-desktop"), None, None, None));
        assert!(f().is_sensitive(Some("记忆面包"), None, None, None));
    }

    #[test]
    fn test_memory_bread_window_is_sensitive() {
        assert!(f().is_sensitive(None, None, None, Some("memory-bread RagPanel")));
        assert!(f().is_sensitive(
            Some("Google Chrome"),
            Some("com.google.Chrome"),
            None,
            Some("MemoryBread Preview 9e3011ca-6991-45a0-9a43-c513c510a6b9 - Google Chrome"),
        ));
        assert!(f().is_sensitive(None, None, None, Some("记忆面包 KnowledgePanel")));
    }

    #[test]
    fn test_extra_blocked_bundle_ids() {
        let filter =
            PrivacyFilter::new().with_extra_blocked_bundle_ids(&["com.tencent.xinWeChat".into()]);
        assert!(filter.is_sensitive(None, Some("com.tencent.xinWeChat"), None, None));
    }

    #[test]
    fn test_all_none_is_not_sensitive() {
        assert!(!f().is_sensitive(None, None, None, None));
    }

    #[test]
    fn test_normal_app_is_not_sensitive() {
        assert!(!f().is_sensitive(
            Some("Feishu"),
            Some("com.feishu.feishu"),
            Some("AXTextField"),
            Some("飞书 - 工作群"),
        ));
    }
}
