//! FTS5 预筛工具
//!
//! 列表页关键词搜索的混合策略：先用 FTS5 MATCH 毫秒级取回候选 rowid 集合，
//! 再在候选上执行原有 LIKE 过滤；当 FTS 表缺失、查询失败或候选被上限截断时，
//! 调用方回退为原有 LIKE 全量扫描，保证行为不回归。

use rusqlite::Connection;

/// 候选 rowid 上限：超过该值说明候选被截断，不能作为硬预筛条件。
pub const DEFAULT_FTS_CANDIDATE_CAP: usize = 2000;

/// 把关键词转义为 FTS5 带引号短语，支持前缀匹配（`"term"*`）。
fn escape_fts_term(term: &str) -> String {
    format!("\"{}\"*", term.replace('"', "\"\""))
}

/// 将多个关键词以 OR 组合成 FTS5 查询串；空输入返回 None。
pub fn build_fts_or_query(terms: &[String]) -> Option<String> {
    let parts: Vec<String> = terms
        .iter()
        .map(|term| term.trim())
        .filter(|term| !term.is_empty())
        .map(escape_fts_term)
        .collect();
    if parts.is_empty() {
        None
    } else {
        Some(parts.join(" OR "))
    }
}

/// 按空白拆分用户输入为关键词列表（供整串 query 的调用方使用）。
pub fn split_query_terms(query: &str) -> Vec<String> {
    query
        .split_whitespace()
        .map(str::trim)
        .filter(|term| !term.is_empty())
        .map(ToOwned::to_owned)
        .collect()
}

/// 通过 FTS5 MATCH 查询候选 rowid。
///
/// 返回 `None` 表示调用方应回退为原有 LIKE 全量扫描，包括以下情况：
/// - FTS 表不存在或查询执行失败（语法/数据异常）；
/// - 候选为空（分词器无法命中，例如中文子串查询）；
/// - 候选数达到 `cap`（被 LIMIT 截断，集合不完整）。
pub fn fts_candidate_ids(
    conn: &Connection,
    fts_table: &str,
    fts_query: &str,
    cap: usize,
) -> Option<Vec<i64>> {
    let exists: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1)",
            rusqlite::params![fts_table],
            |row| row.get(0),
        )
        .ok()?;
    if !exists {
        return None;
    }

    // fts_table 仅为模块内部常量，不接收外部输入
    let sql = format!(
        "SELECT rowid FROM {table} WHERE {table} MATCH ?1 LIMIT ?2",
        table = fts_table
    );
    let mut stmt = conn.prepare(&sql).ok()?;
    let rows = stmt
        .query_map(rusqlite::params![fts_query, cap as i64], |row| {
            row.get::<_, i64>(0)
        })
        .ok()?;

    let mut ids: Vec<i64> = Vec::new();
    for row in rows {
        match row {
            Ok(id) => ids.push(id),
            Err(_) => return None,
        }
    }

    if ids.is_empty() || ids.len() >= cap {
        return None;
    }
    Some(ids)
}

/// 把候选 id 集合渲染为 `id IN (?, ?, ...)` 子句及对应绑定值。
pub fn render_in_clause(ids: &[i64]) -> (String, Vec<Box<dyn rusqlite::ToSql>>) {
    let placeholders = vec!["?"; ids.len()].join(", ");
    let binds: Vec<Box<dyn rusqlite::ToSql>> = ids
        .iter()
        .map(|id| Box::new(*id) as Box<dyn rusqlite::ToSql>)
        .collect();
    (format!("({})", placeholders), binds)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_fts_or_query_escapes_quotes() {
        let terms = vec!["GPU".to_string(), "利用率\"注入".to_string()];
        let query = build_fts_or_query(&terms).unwrap();
        assert_eq!(query, "\"GPU\"* OR \"利用率\"\"注入\"*");
    }

    #[test]
    fn test_build_fts_or_query_filters_empty_terms() {
        let terms = vec!["  ".to_string(), String::new(), "周报".to_string()];
        assert_eq!(build_fts_or_query(&terms).as_deref(), Some("\"周报\"*"));
        assert!(build_fts_or_query(&[]).is_none());
        assert!(build_fts_or_query(&[" ".to_string()]).is_none());
    }

    #[test]
    fn test_split_query_terms() {
        let terms = split_query_terms("  GPU 利用率  ");
        assert_eq!(terms, vec!["GPU".to_string(), "利用率".to_string()]);
        assert!(split_query_terms("   ").is_empty());
    }

    #[test]
    fn test_fts_candidate_ids_missing_table_returns_none() {
        let conn = Connection::open_in_memory().unwrap();
        assert!(fts_candidate_ids(&conn, "not_exist_fts", "\"abc\"*", 10).is_none());
    }

    #[test]
    fn test_fts_candidate_ids_hit_miss_and_cap() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE docs(id INTEGER PRIMARY KEY, body TEXT);
             CREATE VIRTUAL TABLE docs_fts USING fts5(body, content='docs', content_rowid='id');
             CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
                 INSERT INTO docs_fts(rowid, body) VALUES (new.id, new.body);
             END;",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO docs(id, body) VALUES (1, 'GPU utilization report')",
            [],
        )
        .unwrap();
        conn.execute("INSERT INTO docs(id, body) VALUES (2, 'weekly notes')", [])
            .unwrap();

        let ids = fts_candidate_ids(&conn, "docs_fts", "\"gpu\"*", 10).unwrap();
        assert_eq!(ids, vec![1]);

        // 空命中返回 None（调用方回退 LIKE）
        assert!(fts_candidate_ids(&conn, "docs_fts", "\"不存在的中文长词\"*", 10).is_none());

        // 命中数达到 cap 视为截断，返回 None
        assert!(fts_candidate_ids(&conn, "docs_fts", "\"gpu\"* OR \"weekly\"*", 2).is_none());
    }
}
