/// 将文档 URL 归一化为稳定身份。
///
/// query/fragment 通常只表示章节、视图或分享参数，不应把同一份文档拆成多条；
/// scheme、host 和企业文档 ID 的大小写在实际 capture 中也并不稳定，因此统一小写。
pub(crate) fn canonical_document_identity(url: &str) -> Option<String> {
    let trimmed = url.trim();
    if trimmed.is_empty() {
        return None;
    }
    let lowered = trimmed.to_lowercase();
    const DOCUMENT_URL_MARKERS: &[&str] = &[
        "docs.corp",
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
    if !DOCUMENT_URL_MARKERS
        .iter()
        .any(|marker| lowered.contains(marker))
    {
        return None;
    }

    let without_fragment = trimmed.split('#').next().unwrap_or(trimmed);
    let without_query = without_fragment
        .split('?')
        .next()
        .unwrap_or(without_fragment);
    let without_scheme = without_query
        .strip_prefix("https://")
        .or_else(|| without_query.strip_prefix("http://"))
        .unwrap_or(without_query);
    let identity = without_scheme.trim_end_matches('/').trim().to_lowercase();
    (!identity.is_empty()).then_some(identity)
}

/// 标题只作为无 URL 文档的保守兜底身份。
///
/// 这里只消除展示层差异：空白、横线样式、浏览器/编辑器后缀和“云文档”UI 后缀；
/// “修订版”“会议纪要”等有语义的版本词不会被移除。
pub(crate) fn canonical_document_title_identity(title: &str) -> Option<String> {
    let mut normalized = strip_document_title_runtime_suffixes(title)
        .to_lowercase()
        .chars()
        .filter(|ch| !ch.is_whitespace())
        .map(|ch| match ch {
            '–' | '—' | '－' => '-',
            other => other,
        })
        .collect::<String>();

    const UI_SUFFIXES: &[&str] = &[
        "-googlechrome",
        "-microsoftedge",
        "-safari",
        "-firefox",
        "-arc",
        "-microsoftword",
        "-word",
        "-pages",
        "-云文档",
        "（云文档）",
        "(云文档)",
    ];
    loop {
        let Some(suffix) = UI_SUFFIXES
            .iter()
            .find(|suffix| normalized.ends_with(**suffix))
        else {
            break;
        };
        normalized.truncate(normalized.len().saturating_sub(suffix.len()));
        normalized = normalized.trim_end_matches('-').to_string();
    }

    (!normalized.is_empty()).then_some(normalized)
}

/// 从采集窗口标题中提取可用于展示的稳定文档标题。
///
/// 与 `canonical_document_title_identity` 不同，这里保留“ - 云文档”等用户熟悉的
/// 展示后缀，只移除浏览器、编辑器和 Chrome 内存告警等运行时噪声。占位页标题会
/// 返回 `None`，避免“未命名文档 - 云文档”“知识库”覆盖后续捕获到的真实标题。
pub(crate) fn canonical_document_source_title(
    value: &str,
    app_name: Option<&str>,
) -> Option<String> {
    let title = strip_document_title_runtime_suffixes(value);
    if title.is_empty()
        || app_name.is_some_and(|app| title.eq_ignore_ascii_case(app.trim()))
        || is_generic_document_source_title(&title)
    {
        None
    } else {
        Some(title)
    }
}

/// 判断标题是否只是应用/文档容器的占位名称，而非真实文档标题。
pub(crate) fn is_generic_document_source_title(value: &str) -> bool {
    let Some(normalized) = canonical_document_title_identity(value) else {
        return true;
    };
    normalized.chars().count() < 3
        || matches!(
            normalized.as_str(),
            "docs"
                | "document"
                | "documents"
                | "文档"
                | "云文档"
                | "在线文档"
                | "untitled"
                | "untitleddocument"
                | "无标题"
                | "无标题文档"
                | "未命名"
                | "未命名文档"
                | "知识库"
                | "knowledgebase"
                | "googlechrome"
                | "microsoftedge"
                | "safari"
                | "firefox"
                | "microsoftword"
                | "word"
                | "pages"
                | "kim"
                | "chatgpt"
                | "snip"
        )
}

fn strip_document_title_runtime_suffixes(value: &str) -> String {
    let mut title = value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim()
        .to_string();

    loop {
        let lowered = title.to_lowercase();
        let suffix = [
            " - google chrome",
            " - microsoft edge",
            " - safari",
            " - firefox",
            " - arc",
            " - microsoft word",
            " - word",
            " - visual studio code",
            " - cursor",
            " - pages",
        ]
        .into_iter()
        .find(|suffix| lowered.ends_with(suffix));
        let Some(suffix) = suffix else {
            break;
        };
        title.truncate(title.len().saturating_sub(suffix.len()));
        title = title.trim().to_string();
    }

    // Chrome 会把“内存用量高 - 830 MB”插到真实页面标题与浏览器后缀之间。
    // 该数字随运行状态变化，不能成为文档标题或去重身份的一部分。
    if let Some(index) = title.rfind(" - 内存用量高 - ") {
        let usage = title[index + " - 内存用量高 - ".len()..]
            .trim()
            .to_lowercase();
        let numeric = usage
            .strip_suffix(" mb")
            .or_else(|| usage.strip_suffix(" gb"))
            .map(str::trim)
            .filter(|value| {
                !value.is_empty()
                    && value
                        .chars()
                        .all(|ch| ch.is_ascii_digit() || matches!(ch, ',' | '.'))
            })
            .is_some();
        if numeric {
            title.truncate(index);
            title = title.trim().to_string();
        }
    }

    title
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_url_ignores_view_parameters_and_scheme() {
        assert_eq!(
            canonical_document_identity(
                "https://Docs.Corp.Example/d/home/ABC123?section=one#comment"
            ),
            canonical_document_identity("http://docs.corp.example/d/home/abc123/")
        );
    }

    #[test]
    fn canonical_title_only_removes_ui_variants() {
        assert_eq!(
            canonical_document_title_identity("商业体系-AI 建设资产复用方案 - 云文档"),
            canonical_document_title_identity("商业体系-AI建设资产复用方案（云文档）")
        );
        assert_ne!(
            canonical_document_title_identity("商业体系-AI建设资产复用方案（修订版）"),
            canonical_document_title_identity("商业体系-AI建设资产复用方案")
        );
    }

    #[test]
    fn source_title_rejects_compound_placeholders() {
        assert_eq!(
            canonical_document_source_title("未命名文档 - 云文档", Some("Google Chrome")),
            None
        );
        assert_eq!(
            canonical_document_source_title("知识库", Some("Google Chrome")),
            None
        );
        assert_eq!(
            canonical_document_source_title(
                "商业化大模型例行压测介绍 - 云文档 - Google Chrome",
                Some("Google Chrome")
            )
            .as_deref(),
            Some("商业化大模型例行压测介绍 - 云文档")
        );
    }

    #[test]
    fn source_title_removes_chrome_runtime_memory_suffix() {
        assert_eq!(
            canonical_document_source_title(
                "MaaS的一些联想 - 云文档 - 内存用量高 - 830 MB - Google Chrome",
                Some("Google Chrome")
            )
            .as_deref(),
            Some("MaaS的一些联想 - 云文档")
        );
    }
}
