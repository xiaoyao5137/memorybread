//! 采集与后续提炼共享的文本预算契约。
//!
//! 当前提炼运行时使用 32,768 token 上下文，结构化输出预留 8,192 token，
//! 另留 1,024 token 安全余量。16,000 个中文字符按提炼侧 1.35 安全倍率
//! 估算约 14,400 token，加上约 3,200 token 系统 Prompt 和输入结构仍低于
//! 23,552 token 输入预算。超过该值时 AX/DOM 整篇正文会在提炼前被截断，
//! 应改用当前应用截图 OCR 保存用户实际看见的核心段落。

/// 单条 capture 在首次时间线提炼中最多可贡献的字符数。
pub const TIMELINE_EXTRACTION_CAPTURE_TEXT_BUDGET_CHARS: usize = 16_000;
