//! 列表页关键词搜索的统一语义。
//!
//! 用户输入会按空白和常见标点拆成多个关键词。一个结果只有在每个关键词都能在
//! 任一可搜索字段中找到时才命中；关键词不要求相邻，也不要求出现在同一字段。

use std::collections::HashSet;

fn is_query_separator(ch: char) -> bool {
    ch.is_whitespace()
        || ch.is_ascii_punctuation()
        || "，。；：、！？（）【】《》〈〉“”‘’「」『』·".contains(ch)
}

/// 拆分、统一小写并去重。保留用户输入顺序，便于稳定评分与调试。
pub fn split_search_terms(query: &str) -> Vec<String> {
    let mut seen = HashSet::new();
    query
        .split(is_query_separator)
        .map(str::trim)
        .filter(|term| !term.is_empty())
        .map(str::to_lowercase)
        .filter(|term| seen.insert(term.clone()))
        .collect()
}

fn normalized_phrase(query: &str) -> String {
    query
        .split_whitespace()
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn joined_lower(fields: &[&str]) -> String {
    fields
        .iter()
        .map(|field| field.to_lowercase())
        .collect::<Vec<_>>()
        .join("\n")
}

/// 返回匹配分；`None` 表示至少一个关键词缺失。
///
/// 分档顺序：标题完整短语 > 全部关键词均在标题 > 标题与元数据覆盖全部关键词
/// > 需要正文才能覆盖。调用方可在同分时继续按时间或业务热度排序。
pub fn search_match_score(
    query: &str,
    terms: &[String],
    title_fields: &[&str],
    metadata_fields: &[&str],
    body_fields: &[&str],
) -> Option<i64> {
    if terms.is_empty() {
        return Some(0);
    }

    let title = joined_lower(title_fields);
    let metadata = joined_lower(metadata_fields);
    let body = joined_lower(body_fields);
    let all_fields = format!("{}\n{}\n{}", title, metadata, body);
    if !terms.iter().all(|term| all_fields.contains(term)) {
        return None;
    }

    let phrase = normalized_phrase(query);
    let title_hits = terms.iter().filter(|term| title.contains(*term)).count() as i64;
    let metadata_hits = terms.iter().filter(|term| metadata.contains(*term)).count() as i64;
    let base = if !phrase.is_empty() && title.contains(&phrase) {
        4_000
    } else if terms.iter().all(|term| title.contains(term)) {
        3_000
    } else {
        let title_and_metadata = format!("{}\n{}", title, metadata);
        if terms.iter().all(|term| title_and_metadata.contains(term)) {
            2_000
        } else {
            1_000
        }
    };
    Some(base + title_hits * 20 + metadata_hits * 5)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_whitespace_and_common_punctuation_and_deduplicates() {
        assert_eq!(
            split_search_terms(" AIGC、图生视频 RPC，aigc "),
            vec!["aigc", "图生视频", "rpc"]
        );
        assert!(split_search_terms("，。、 / ").is_empty());
    }

    #[test]
    fn matches_all_terms_without_requiring_adjacency_or_order() {
        let terms = split_search_terms("AIGC 图生视频 接入文档");
        assert!(search_match_score(
            "AIGC 图生视频 接入文档",
            &terms,
            &["AIGC 图生视频 RPC 接入文档"],
            &[],
            &[],
        )
        .is_some());
        assert!(search_match_score(
            "AIGC 图生视频 接入文档",
            &terms,
            &["AIGC 图生视频 RPC 说明"],
            &[],
            &[],
        )
        .is_none());
    }

    #[test]
    fn ranks_exact_title_phrase_above_distributed_matches() {
        let query = "GPU 接入";
        let terms = split_search_terms(query);
        let exact = search_match_score(query, &terms, &["GPU 接入"], &[], &[]).unwrap();
        let title_terms = search_match_score(query, &terms, &["GPU RPC 接入"], &[], &[]).unwrap();
        let distributed = search_match_score(query, &terms, &["GPU"], &["接入"], &[]).unwrap();
        assert!(exact > title_terms);
        assert!(title_terms > distributed);
    }
}
