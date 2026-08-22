//! 烘焙文档的浏览器即时刷新资格判定。
//!
//! 与数据侧 `refresh_required` 的设计保持一致：判定是纯确定性规则，不依赖
//! 模型，也不使用业务关键词枚举。判定输入只有三类：
//! 1. 显式策略 `refresh_policy`（never/always/auto，用户可覆盖）；
//! 2. 文档自身的语料统计证据（来源正文指纹数量与更新节奏）；
//! 3. 时效与节流（检查间隔、内容 TTL）。
//!
//! auto 策略下必须存在“原地更新证据”才允许刷新：
//! - 同一 URL 观察到过 ≥2 个不同的来源正文指纹（用户确实看到过内容变化）；
//! - 或 URL 属于已知的在线协作文档平台（周报/月报类文档常只被观察过一次，
//!   单指纹不足以否定其周期性更新；平台判定复用 062 迁移的身份模式集）。

use sha2::{Digest, Sha256};

use crate::storage::models_bake::BakeDocumentRecord;

/// 两次浏览器新鲜度检查之间的最小间隔：创作频繁触发召回时，
/// 不能对同一文档反复打开浏览器。
pub const DOCUMENT_REFRESH_CHECK_INTERVAL_MS: i64 = 6 * 3600 * 1000;
/// 失败后的短退避：页面超时或浏览器瞬态故障不应占用完整 6 小时节流窗口，
/// 否则下一次明确要求最新内容的创作仍然只能消费历史版本。
pub const DOCUMENT_REFRESH_FAILED_RETRY_INTERVAL_MS: i64 = 10 * 60 * 1000;
/// 无更新节奏证据时的默认内容 TTL。
pub const DOCUMENT_REFRESH_DEFAULT_TTL_MS: i64 = 24 * 3600 * 1000;
/// 由观察节奏推导 TTL 时的上下界：不低于 12h（避免高频打扰），
/// 不高于 7d（失去“及时”意义）。
pub const DOCUMENT_REFRESH_MIN_TTL_MS: i64 = 12 * 3600 * 1000;
pub const DOCUMENT_REFRESH_MAX_TTL_MS: i64 = 7 * 24 * 3600 * 1000;
/// TTL 取观察节奏的一半：在下次预期更新到来前提前检查即可，
/// 无需每个检查窗口都打开页面。
const DOCUMENT_REFRESH_TTL_CADENCE_FACTOR: f64 = 0.5;
/// 至少需要 3 个指纹（2 个间隔）才认为节奏可估计。
const DOCUMENT_REFRESH_MIN_CADENCE_SAMPLES: usize = 3;
/// 页面已不存在的终态错误码：一旦确认，永久阻止后续刷新。
pub const DOCUMENT_REFRESH_ERROR_PAGE_GONE: &str = "PAGE_GONE";

pub const DOCUMENT_REFRESH_POLICY_AUTO: &str = "auto";
pub const DOCUMENT_REFRESH_POLICY_ALWAYS: &str = "always";
pub const DOCUMENT_REFRESH_POLICY_NEVER: &str = "never";

/// 资格判定的结论：要么给出本次允许刷新所依据的内容 TTL，
/// 要么给出可落库、可展示的跳过原因。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DocumentRefreshDecision {
    Due { content_ttl_ms: i64 },
    Skip(DocumentRefreshSkipReason),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DocumentRefreshSkipReason {
    PolicyNever,
    UrlMissing,
    UrlInvalid,
    PageGone,
    CheckThrottled,
    /// auto 策略下缺少“原地更新证据”。
    NoUpdateEvidence,
    ContentFresh,
}

impl DocumentRefreshSkipReason {
    pub fn as_str(&self) -> &'static str {
        match self {
            DocumentRefreshSkipReason::PolicyNever => "policy_never",
            DocumentRefreshSkipReason::UrlMissing => "url_missing",
            DocumentRefreshSkipReason::UrlInvalid => "url_invalid",
            DocumentRefreshSkipReason::PageGone => "page_gone",
            DocumentRefreshSkipReason::CheckThrottled => "check_throttled",
            DocumentRefreshSkipReason::NoUpdateEvidence => "no_update_evidence",
            DocumentRefreshSkipReason::ContentFresh => "content_fresh",
        }
    }
}

pub fn is_valid_document_refresh_policy(policy: &str) -> bool {
    matches!(
        policy,
        DOCUMENT_REFRESH_POLICY_AUTO
            | DOCUMENT_REFRESH_POLICY_ALWAYS
            | DOCUMENT_REFRESH_POLICY_NEVER
    )
}

/// 来源正文指纹：与 bake 流水线的 `artifact_source_fingerprint` 完全同构，
/// 保证浏览器刷新抓到的正文与历史 capture 的指纹可以直接比较。
pub fn source_text_fingerprint(source_text: &str) -> Option<String> {
    let normalized = source_text
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase();
    if normalized.is_empty() {
        return None;
    }
    let mut hasher = Sha256::new();
    hasher.update(normalized.as_bytes());
    Some(format!("source-v1:{:x}", hasher.finalize()))
}

/// 刷新用 URL 的合法性判定：与数据侧 `validate_scrape_url` 同一套规则，
/// 只允许不含凭据的 HTTP/HTTPS 地址，避免把内嵌拼接等畸形 URL 送进浏览器。
pub fn is_refreshable_document_url(url: &str) -> bool {
    match reqwest::Url::parse(url.trim()) {
        Ok(parsed) => {
            matches!(parsed.scheme(), "http" | "https")
                && parsed.username().is_empty()
                && parsed.password().is_none()
        }
        Err(_) => false,
    }
}

/// 在线协作文档平台的 URL 形态判定。模式集与 062 迁移的文档身份
/// 归一化保持一致：这是“页面形态”而非“业务名称”的判定，可用于任意站点。
pub fn looks_like_live_document_url(url: &str) -> bool {
    let lowered = url.to_lowercase();
    const PATTERNS: &[&str] = &[
        "/docs/",
        "docs.google",
        "/document/",
        "yuque.com",
        "feishu.cn/docx",
        "feishu.cn/wiki",
        "larkoffice.com/wiki",
        "notion.so",
        "confluence",
        "/wiki/",
        "shimo.im",
        "/d/home/",
        "/s/home/",
        "/k/home/",
    ];
    PATTERNS.iter().any(|pattern| lowered.contains(pattern))
}

/// 从指纹观察时间序列推导内容 TTL：更新节奏的中位间隔 × 0.5，
/// 再 clamp 到 [12h, 7d]；样本不足时使用默认 24h。
pub fn document_refresh_ttl_ms(fingerprint_observed_at: &[i64]) -> i64 {
    if fingerprint_observed_at.len() < DOCUMENT_REFRESH_MIN_CADENCE_SAMPLES {
        return DOCUMENT_REFRESH_DEFAULT_TTL_MS;
    }
    let mut gaps: Vec<i64> = fingerprint_observed_at
        .windows(2)
        .map(|window| window[1].saturating_sub(window[0]))
        .filter(|gap| *gap > 0)
        .collect();
    if gaps.is_empty() {
        return DOCUMENT_REFRESH_DEFAULT_TTL_MS;
    }
    gaps.sort_unstable();
    let median_gap = gaps[gaps.len() / 2];
    let ttl = (median_gap as f64 * DOCUMENT_REFRESH_TTL_CADENCE_FACTOR) as i64;
    ttl.clamp(DOCUMENT_REFRESH_MIN_TTL_MS, DOCUMENT_REFRESH_MAX_TTL_MS)
}

/// 刷新资格判定主入口。`url_valid` 由调用方用 URL 校验器预先判定，
/// 保持与数据源刷新同一套 URL 规则（拒绝内嵌拼接等畸形地址）。
pub fn evaluate_document_refresh(
    doc: &BakeDocumentRecord,
    fingerprints: &[(String, i64)],
    now_ms: i64,
    url_valid: bool,
) -> DocumentRefreshDecision {
    if doc.refresh_policy == DOCUMENT_REFRESH_POLICY_NEVER {
        return DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::PolicyNever);
    }
    let has_url = doc
        .source_url
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .is_some();
    if !has_url {
        return DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::UrlMissing);
    }
    if !url_valid {
        return DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::UrlInvalid);
    }
    if doc.last_refresh_error.as_deref() == Some(DOCUMENT_REFRESH_ERROR_PAGE_GONE) {
        return DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::PageGone);
    }
    let check_interval_ms = if doc.last_refresh_error.is_some() {
        DOCUMENT_REFRESH_FAILED_RETRY_INTERVAL_MS
    } else {
        DOCUMENT_REFRESH_CHECK_INTERVAL_MS
    };
    if now_ms.saturating_sub(doc.last_refresh_checked_at_ms) < check_interval_ms {
        return DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::CheckThrottled);
    }
    if doc.refresh_policy != DOCUMENT_REFRESH_POLICY_ALWAYS {
        // auto：必须有语料统计层面的“原地更新证据”，避免把一次性静态页面
        // 纳入周期性浏览器刷新。
        let has_observed_change = fingerprints.len() >= 2;
        let on_live_platform = doc
            .source_url
            .as_deref()
            .map(looks_like_live_document_url)
            .unwrap_or(false);
        if !has_observed_change && !on_live_platform {
            return DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::NoUpdateEvidence);
        }
    }
    let observed_at = fingerprints
        .iter()
        .map(|(_, created_at)| *created_at)
        .max()
        .unwrap_or(doc.created_at);
    let ttl = document_refresh_ttl_ms(
        &fingerprints
            .iter()
            .map(|(_, created_at)| *created_at)
            .collect::<Vec<_>>(),
    );
    if now_ms.saturating_sub(observed_at) < ttl {
        return DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::ContentFresh);
    }
    DocumentRefreshDecision::Due {
        content_ttl_ms: ttl,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::storage::models_bake::NewBakeDocument;

    fn doc_with(policy: &str, source_url: Option<&str>) -> BakeDocumentRecord {
        let new_doc = NewBakeDocument::with_defaults("测试文档".to_string(), "周报".to_string());
        let mut record: BakeDocumentRecord = serde_json::from_value(serde_json::json!({
            "id": 1,
            "title": new_doc.title,
            "doc_type": new_doc.doc_type,
            "status": new_doc.status,
            "tags": new_doc.tags,
            "applicable_tasks": new_doc.applicable_tasks,
            "source_memory_ids": new_doc.source_memory_ids,
            "source_capture_ids": new_doc.source_capture_ids,
            "source_episode_ids": new_doc.source_episode_ids,
            "linked_knowledge_ids": new_doc.linked_knowledge_ids,
            "sections_json": new_doc.sections_json,
            "style_phrases": new_doc.style_phrases,
            "replacement_rules": new_doc.replacement_rules,
            "summary": null,
            "full_content": null,
            "structured_content": new_doc.structured_content,
            "prompt_hint": null,
            "diagram_code": null,
            "image_assets": new_doc.image_assets,
            "source_app_name": null,
            "source_win_title": null,
            "source_url": source_url,
            "content_hash": null,
            "language": null,
            "usage_count": 0,
            "match_score": null,
            "match_level": null,
            "creation_mode": "llm_bake",
            "review_status": "auto_created",
            "evidence_summary": null,
            "generation_version": null,
            "refresh_policy": policy,
            "last_refresh_checked_at_ms": 0,
            "last_refresh_error": null,
            "last_refresh_success_at_ms": 0,
            "last_refresh_status": "historical_only",
            "last_refresh_completeness": "unverified",
            "last_refresh_content_hash": null,
            "last_refresh_character_count": 0,
            "last_refresh_segment_count": 0,
            "last_refresh_truncated": false,
            "deleted_at": null,
            "created_at": 0,
            "updated_at": 0,
        }))
        .unwrap();
        record.source_url = source_url.map(|value| value.to_string());
        record.refresh_policy = policy.to_string();
        record
    }

    const HOUR_MS: i64 = 3600 * 1000;
    const FEISHU_URL: &str = "https://acme.feishu.cn/wiki/AbCdEfGh";

    #[test]
    fn policy_never_blocks_refresh_even_when_stale() {
        let doc = doc_with("never", Some(FEISHU_URL));
        let decision = evaluate_document_refresh(&doc, &[], 100 * HOUR_MS, true);
        assert_eq!(
            decision,
            DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::PolicyNever)
        );
    }

    #[test]
    fn missing_or_invalid_url_blocks_refresh() {
        let doc = doc_with("auto", None);
        assert_eq!(
            evaluate_document_refresh(&doc, &[], 100 * HOUR_MS, true),
            DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::UrlMissing)
        );
        let doc = doc_with("auto", Some("https://docs.example.com/a?u=https://evil"));
        assert_eq!(
            evaluate_document_refresh(&doc, &[], 100 * HOUR_MS, false),
            DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::UrlInvalid)
        );
    }

    #[test]
    fn page_gone_error_permanently_blocks_refresh() {
        let mut doc = doc_with("always", Some(FEISHU_URL));
        doc.last_refresh_error = Some(DOCUMENT_REFRESH_ERROR_PAGE_GONE.to_string());
        assert_eq!(
            evaluate_document_refresh(&doc, &[], 100 * HOUR_MS, true),
            DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::PageGone)
        );
    }

    #[test]
    fn recent_check_is_throttled() {
        let mut doc = doc_with("always", Some(FEISHU_URL));
        let now = 100 * HOUR_MS;
        doc.last_refresh_checked_at_ms = now - DOCUMENT_REFRESH_CHECK_INTERVAL_MS + 1;
        assert_eq!(
            evaluate_document_refresh(&doc, &[], now, true),
            DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::CheckThrottled)
        );
    }

    #[test]
    fn transient_failure_uses_short_retry_interval() {
        let mut doc = doc_with("always", Some(FEISHU_URL));
        let now = 100 * HOUR_MS;
        doc.last_refresh_error = Some("SCRAPE_TIMEOUT".to_string());
        doc.last_refresh_checked_at_ms = now - DOCUMENT_REFRESH_FAILED_RETRY_INTERVAL_MS - 1;
        assert_eq!(
            evaluate_document_refresh(&doc, &[], now, true),
            DocumentRefreshDecision::Due {
                content_ttl_ms: DOCUMENT_REFRESH_DEFAULT_TTL_MS,
            }
        );
    }

    #[test]
    fn transient_failure_is_still_throttled_during_short_backoff() {
        let mut doc = doc_with("always", Some(FEISHU_URL));
        let now = 100 * HOUR_MS;
        doc.last_refresh_error = Some("SCRAPE_TIMEOUT".to_string());
        doc.last_refresh_checked_at_ms = now - DOCUMENT_REFRESH_FAILED_RETRY_INTERVAL_MS + 1;
        assert_eq!(
            evaluate_document_refresh(&doc, &[], now, true),
            DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::CheckThrottled)
        );
    }

    #[test]
    fn auto_policy_requires_in_place_update_evidence() {
        // 单指纹 + 非协作平台 URL：没有证据，不刷新
        let doc = doc_with("auto", Some("https://static.example.com/report.html"));
        let fingerprints = vec![("source-v1:a".to_string(), 10 * HOUR_MS)];
        assert_eq!(
            evaluate_document_refresh(&doc, &fingerprints, 100 * HOUR_MS, true),
            DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::NoUpdateEvidence)
        );
        // 同一 URL 观察到两个不同内容版本：强证据，允许刷新
        let fingerprints = vec![
            ("source-v1:a".to_string(), 10 * HOUR_MS),
            ("source-v1:b".to_string(), 40 * HOUR_MS),
        ];
        assert_eq!(
            evaluate_document_refresh(&doc, &fingerprints, 100 * HOUR_MS, true),
            DocumentRefreshDecision::Due {
                content_ttl_ms: DOCUMENT_REFRESH_DEFAULT_TTL_MS
            }
        );
        // 单指纹但 URL 是协作平台：周期性文档常只被观察一次，允许刷新
        let doc = doc_with("auto", Some(FEISHU_URL));
        let fingerprints = vec![("source-v1:a".to_string(), 10 * HOUR_MS)];
        assert_eq!(
            evaluate_document_refresh(&doc, &fingerprints, 100 * HOUR_MS, true),
            DocumentRefreshDecision::Due {
                content_ttl_ms: DOCUMENT_REFRESH_DEFAULT_TTL_MS
            }
        );
    }

    #[test]
    fn refreshable_url_validation_matches_scrape_rules() {
        assert!(is_refreshable_document_url(FEISHU_URL));
        assert!(is_refreshable_document_url("http://example.com/a"));
        assert!(!is_refreshable_document_url("file:///tmp/a.html"));
        assert!(!is_refreshable_document_url(
            "https://user:pass@example.com/a"
        ));
        assert!(!is_refreshable_document_url("not a url"));
        assert!(!is_refreshable_document_url("  "));
    }

    #[test]
    fn fresh_content_is_not_refreshed_until_ttl_expires() {
        let doc = doc_with("auto", Some(FEISHU_URL));
        let now = 100 * HOUR_MS;
        let fingerprints = vec![("source-v1:a".to_string(), now - 2 * HOUR_MS)];
        assert_eq!(
            evaluate_document_refresh(&doc, &fingerprints, now, true),
            DocumentRefreshDecision::Skip(DocumentRefreshSkipReason::ContentFresh)
        );
        let fingerprints = vec![("source-v1:a".to_string(), now - 30 * HOUR_MS)];
        assert!(matches!(
            evaluate_document_refresh(&doc, &fingerprints, now, true),
            DocumentRefreshDecision::Due { .. }
        ));
    }

    #[test]
    fn ttl_follows_observed_cadence_within_bounds() {
        // 每周更新一次（7d 间隔）→ TTL = 3.5d
        let observations = vec![0, 7 * 24, 14 * 24]
            .into_iter()
            .map(|hours| hours * HOUR_MS)
            .collect::<Vec<_>>();
        let observed = observations.as_slice();
        assert_eq!(
            document_refresh_ttl_ms(observed),
            (3 * 24 * HOUR_MS + 12 * HOUR_MS) as i64
        );
        // 高频更新被下限 clamp 到 12h
        let observations = vec![0, HOUR_MS, 2 * HOUR_MS];
        assert_eq!(
            document_refresh_ttl_ms(observations.as_slice()),
            DOCUMENT_REFRESH_MIN_TTL_MS
        );
        // 低频更新被上限 clamp 到 7d
        let observations = vec![0, 40 * 24 * HOUR_MS, 80 * 24 * HOUR_MS];
        assert_eq!(
            document_refresh_ttl_ms(observations.as_slice()),
            DOCUMENT_REFRESH_MAX_TTL_MS
        );
        // 样本不足走默认 TTL
        let observations = vec![0, 24 * HOUR_MS];
        assert_eq!(
            document_refresh_ttl_ms(observations.as_slice()),
            DOCUMENT_REFRESH_DEFAULT_TTL_MS
        );
    }

    #[test]
    fn fingerprint_matches_bake_normalization() {
        let left = source_text_fingerprint("本周 订单\n1200 单").unwrap();
        let right = source_text_fingerprint(" 本周 订单 1200 单 ").unwrap();
        assert_eq!(left, right);
        assert!(source_text_fingerprint("   ").is_none());
    }
}
