//! 内容级隐私过滤器。
//!
//! 与应用黑名单不同，这里不丢弃整条 capture，而是把敏感片段替换为
//! `[已过滤]`，让后续提炼和向量化只消费脱敏文本。

use std::collections::BTreeSet;

use serde_json::Value;

use crate::storage::{repo::privacy, StorageManager};

const REDACTION: &str = "[已过滤]";

#[derive(Debug, Clone, Default)]
pub struct ContentFilterResult {
    pub text: String,
    pub hit_types: Vec<String>,
    pub redacted_count: usize,
    pub redacted_characters: usize,
}

#[derive(Debug, Clone)]
struct FilterRule {
    filter_type: String,
    config: Value,
}

#[derive(Debug, Clone)]
pub struct ContentFilter {
    rules: Vec<FilterRule>,
}

impl ContentFilter {
    pub fn from_storage(storage: &StorageManager) -> Self {
        let rules = storage
            .with_conn(|conn| privacy::get_enabled_privacy_filters(conn))
            .map(|records| {
                records
                    .into_iter()
                    .filter_map(|record| {
                        let config = record
                            .config_json
                            .as_deref()
                            .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
                            .unwrap_or(Value::Null);
                        Some(FilterRule {
                            filter_type: record.filter_type,
                            config,
                        })
                    })
                    .collect()
            })
            .unwrap_or_default();

        Self { rules }
    }

    pub fn filter_text(&self, text: &str) -> ContentFilterResult {
        let mut ranges: Vec<(usize, usize, String)> = Vec::new();

        for rule in &self.rules {
            match rule.filter_type.as_str() {
                "chat" => {
                    collect_keyword_ranges(&rule.config, text, &rule.filter_type, &mut ranges);
                    collect_chat_pattern_ranges(&rule.config, text, &rule.filter_type, &mut ranges);
                }
                "pii" => collect_pii_ranges(&rule.config, text, &rule.filter_type, &mut ranges),
                "policy" => {
                    collect_policy_ranges(&rule.config, text, &rule.filter_type, &mut ranges)
                }
                _ => collect_keyword_ranges(&rule.config, text, &rule.filter_type, &mut ranges),
            }
        }

        redact_ranges(text, ranges)
    }
}

fn collect_keyword_ranges(
    config: &Value,
    text: &str,
    filter_type: &str,
    ranges: &mut Vec<(usize, usize, String)>,
) {
    for keyword in string_array(config.get("keywords")) {
        if keyword.is_empty() {
            continue;
        }
        for (start, _) in text.match_indices(&keyword) {
            ranges.push((start, start + keyword.len(), filter_type.to_string()));
        }
    }
}

fn collect_policy_ranges(
    config: &Value,
    text: &str,
    filter_type: &str,
    ranges: &mut Vec<(usize, usize, String)>,
) {
    let context_window = config
        .get("context_window")
        .and_then(Value::as_u64)
        .unwrap_or(50) as usize;

    for keyword in string_array(config.get("keywords")) {
        if keyword.is_empty() {
            continue;
        }
        for (start, _) in text.match_indices(&keyword) {
            let end = start + keyword.len();
            ranges.push((
                floor_char_boundary(text, start.saturating_sub(context_window)),
                ceil_char_boundary(text, (end + context_window).min(text.len())),
                filter_type.to_string(),
            ));
        }
    }
}

fn collect_chat_pattern_ranges(
    config: &Value,
    text: &str,
    filter_type: &str,
    ranges: &mut Vec<(usize, usize, String)>,
) {
    for pattern in string_array(config.get("patterns")) {
        if pattern.contains("验证码") {
            collect_keyword_value_range(text, "验证码", true, filter_type, ranges);
        } else if pattern.contains("密码") {
            collect_keyword_value_range(text, "密码", false, filter_type, ranges);
        } else if pattern.contains("账号") {
            collect_keyword_value_range(text, "账号", false, filter_type, ranges);
        }
    }
}

fn collect_keyword_value_range(
    text: &str,
    keyword: &str,
    digits_only: bool,
    filter_type: &str,
    ranges: &mut Vec<(usize, usize, String)>,
) {
    for (keyword_start, _) in text.match_indices(keyword) {
        // Match the configured field label, not the suffix of a different
        // label (for example 主账号). More labels can be configured explicitly.
        if keyword == "账号" && text[..keyword_start].chars().next_back()
            .is_some_and(|ch| ch.is_alphanumeric() || ch == '_') {
            continue;
        }
        let mut cursor = keyword_start + keyword.len();
        // A configured credential field requires its separator. A mention such
        // as 商家账号运营 is prose, not a field whose value extends to line end.
        // Do not cross block boundaries while looking for a separator/value.
        while text[cursor..].starts_with([' ', '\t']) {
            cursor += 1;
        }
        let Some(separator) = text[cursor..].chars().next() else {
            continue;
        };
        if !matches!(separator, ':' | '：') {
            continue;
        }
        cursor += separator.len_utf8();
        while text[cursor..].starts_with([' ', '\t']) {
            cursor += 1;
        }

        let mut end = cursor;
        while end < text.len() {
            let ch = text[end..].chars().next().unwrap();
            if ch == '\n' || ch == '\r' {
                break;
            }
            if digits_only && !ch.is_ascii_digit() {
                break;
            }
            end += ch.len_utf8();
        }

        if end > cursor {
            ranges.push((keyword_start, end, filter_type.to_string()));
        }
    }
}

fn collect_pii_ranges(
    config: &Value,
    text: &str,
    filter_type: &str,
    ranges: &mut Vec<(usize, usize, String)>,
) {
    let patterns = config.get("patterns").unwrap_or(&Value::Null);
    let has_pattern = |key: &str| patterns.get(key).and_then(Value::as_str).is_some();

    if has_pattern("phone") {
        collect_phone_ranges(text, filter_type, ranges);
    }
    if has_pattern("id_card") {
        collect_id_card_ranges(text, filter_type, ranges);
    }
    if has_pattern("bank_card") {
        collect_bank_card_ranges(text, filter_type, ranges);
    }
    if has_pattern("email") {
        collect_email_ranges(text, filter_type, ranges);
    }
}

fn collect_phone_ranges(text: &str, filter_type: &str, ranges: &mut Vec<(usize, usize, String)>) {
    for (start, token) in ascii_digit_runs(text, 11, 11) {
        let bytes = token.as_bytes();
        if bytes[0] == b'1' && matches!(bytes[1], b'3'..=b'9') {
            ranges.push((start, start + token.len(), filter_type.to_string()));
        }
    }
}

fn collect_id_card_ranges(text: &str, filter_type: &str, ranges: &mut Vec<(usize, usize, String)>) {
    for (start, token) in ascii_alnum_runs(text, 18, 18) {
        let last_ok = token
            .as_bytes()
            .last()
            .is_some_and(|b| b.is_ascii_digit() || *b == b'X' || *b == b'x');
        if token[..17].bytes().all(|b| b.is_ascii_digit()) && last_ok {
            ranges.push((start, start + token.len(), filter_type.to_string()));
        }
    }
}

fn collect_bank_card_ranges(
    text: &str,
    filter_type: &str,
    ranges: &mut Vec<(usize, usize, String)>,
) {
    for (start, token) in ascii_digit_runs(text, 16, 19) {
        ranges.push((start, start + token.len(), filter_type.to_string()));
    }
}

fn collect_email_ranges(text: &str, filter_type: &str, ranges: &mut Vec<(usize, usize, String)>) {
    for (start, token) in ascii_visible_runs(text) {
        if token.contains('@') && token.rsplit_once('.').is_some() {
            ranges.push((start, start + token.len(), filter_type.to_string()));
        }
    }
}

fn redact_ranges(text: &str, mut ranges: Vec<(usize, usize, String)>) -> ContentFilterResult {
    if ranges.is_empty() {
        return ContentFilterResult {
            text: text.to_string(),
            hit_types: Vec::new(),
            redacted_count: 0,
            redacted_characters: 0,
        };
    }

    ranges.sort_by_key(|(start, end, _)| (*start, *end));
    let mut merged: Vec<(usize, usize, BTreeSet<String>)> = Vec::new();

    for (start, end, filter_type) in ranges {
        if start >= end || end > text.len() {
            continue;
        }
        if let Some((_, last_end, types)) = merged.last_mut() {
            if start <= *last_end {
                *last_end = (*last_end).max(end);
                types.insert(filter_type);
                continue;
            }
        }
        let mut types = BTreeSet::new();
        types.insert(filter_type);
        merged.push((start, end, types));
    }

    let mut result = String::with_capacity(text.len());
    let mut last_end = 0;
    let mut hit_types = BTreeSet::new();

    for (start, end, types) in &merged {
        result.push_str(&text[last_end..*start]);
        result.push_str(REDACTION);
        last_end = *end;
        hit_types.extend(types.iter().cloned());
    }
    result.push_str(&text[last_end..]);

    ContentFilterResult {
        text: result,
        hit_types: hit_types.into_iter().collect(),
        redacted_count: merged.len(),
        redacted_characters: merged.iter().map(|(start,end,_)|text[*start..*end].chars().count()).sum(),
    }
}

fn string_array(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn ascii_digit_runs(text: &str, min_len: usize, max_len: usize) -> Vec<(usize, &str)> {
    ascii_runs_by(text, min_len, max_len, |b| b.is_ascii_digit())
}

fn ascii_alnum_runs(text: &str, min_len: usize, max_len: usize) -> Vec<(usize, &str)> {
    ascii_runs_by(text, min_len, max_len, |b| b.is_ascii_alphanumeric())
}

fn ascii_visible_runs(text: &str) -> Vec<(usize, &str)> {
    ascii_runs_by(text, 3, usize::MAX, |b| {
        b.is_ascii_alphanumeric() || matches!(b, b'@' | b'.' | b'_' | b'%' | b'+' | b'-')
    })
}

fn ascii_runs_by<F>(text: &str, min_len: usize, max_len: usize, pred: F) -> Vec<(usize, &str)>
where
    F: Fn(u8) -> bool,
{
    let mut runs = Vec::new();
    let bytes = text.as_bytes();
    let mut start: Option<usize> = None;

    for (idx, byte) in bytes.iter().enumerate() {
        if pred(*byte) {
            start.get_or_insert(idx);
        } else if let Some(s) = start.take() {
            push_ascii_run(text, s, idx, min_len, max_len, &mut runs);
        }
    }
    if let Some(s) = start {
        push_ascii_run(text, s, text.len(), min_len, max_len, &mut runs);
    }
    runs
}

fn push_ascii_run<'a>(
    text: &'a str,
    start: usize,
    end: usize,
    min_len: usize,
    max_len: usize,
    runs: &mut Vec<(usize, &'a str)>,
) {
    let len = end - start;
    if len >= min_len && len <= max_len {
        runs.push((start, &text[start..end]));
    }
}

fn floor_char_boundary(text: &str, mut index: usize) -> usize {
    while index > 0 && !text.is_char_boundary(index) {
        index -= 1;
    }
    index
}

fn ceil_char_boundary(text: &str, mut index: usize) -> usize {
    while index < text.len() && !text.is_char_boundary(index) {
        index += 1;
    }
    index
}

#[cfg(test)]
mod tests {
    use super::*;

    fn filter() -> ContentFilter {
        ContentFilter {
            rules: vec![FilterRule {
                filter_type: "pii".into(),
                config: serde_json::json!({
                    "patterns": {
                        "phone": "1[3-9]\\d{9}",
                        "id_card": "\\d{17}[0-9Xx]",
                        "bank_card": "\\d{16,19}",
                        "email": "email"
                    }
                }),
            }],
        }
    }

    #[test]
    fn credential_patterns_preserve_business_account_prose() {
        let filter = ContentFilter {
            rules: vec![FilterRule {
                filter_type: "chat".into(),
                config: serde_json::json!({"patterns": ["账号[:：]", "密码[:：]", "验证码[:：]"]}),
            }],
        };
        let prose = "核心策略：商家账号运营与成熟的账号矩阵，后续应保留完整业务正文。";
        assert_eq!(filter.filter_text(prose).text, prose);
        assert_eq!(filter.filter_text("● 主账号 ：保留商家原有人设与历史视频。").text,
                   "● 主账号 ：保留商家原有人设与历史视频。");
        assert_eq!(filter.filter_text("账号：merchant_123\n业务正文保留").text,
                   "[已过滤]\n业务正文保留");
        assert_eq!(filter.filter_text("密码: secret with spaces\n业务正文保留").text,
                   "[已过滤]\n业务正文保留");
        assert_eq!(filter.filter_text("验证码：123456 用于登录").text,
                   "[已过滤] 用于登录");
        assert_eq!(filter.filter_text("账号\n：业务正文").text, "账号\n：业务正文");
    }

    #[test]
    fn redaction_statistics_count_unicode_union_not_replacement_length() {
        let result = redact_ranges("甲乙丙丁", vec![
            (0, 6, "first".into()), (3, 9, "overlap".into()),
        ]);
        assert_eq!(result.text, "[已过滤]丁");
        assert_eq!(result.redacted_count, 1);
        assert_eq!(result.redacted_characters, 3);
        assert_eq!(redact_ranges("正常正文", vec![]).redacted_characters, 0);
    }

    #[test]
    fn redacts_pii() {
        let result = filter().filter_text("手机号 13800138000 邮箱 a@test.com");
        assert_eq!(result.text, "手机号 [已过滤] 邮箱 [已过滤]");
        assert_eq!(result.hit_types, vec!["pii"]);
        assert_eq!(result.redacted_count, 2);
        assert_eq!(result.redacted_characters, 21);
    }
}
