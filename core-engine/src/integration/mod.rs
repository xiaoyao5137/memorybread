use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
};

use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::storage::{
    ImportWriteOutcome, ImportedKnowledgeItem, IntegrationSkillRunRecord, StorageManager,
};

const MAX_INPUT_FILES: usize = 512;
const MAX_INPUT_BYTES: usize = 24 * 1024 * 1024;
const MAX_FILE_BYTES: usize = 5 * 1024 * 1024;
const MAX_IMPORTED_CONTENT_CHARS: usize = 200_000;
const MAX_EXPORT_MEMORIES: usize = 200;
const MAX_EXPORT_NOTE_CHARS: usize = 200_000;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct IntegrationSkillCatalogItem {
    pub id: &'static str,
    pub title: &'static str,
    pub eyebrow: &'static str,
    pub description: &'static str,
    pub capability: &'static str,
    pub badge: &'static str,
    pub direction: &'static str,
    pub executor: &'static str,
    pub version: &'static str,
    pub input_kind: &'static str,
    pub accept: &'static str,
    pub supports_preview: bool,
    pub file_count: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct IntegrationSkillSourceFile {
    pub path: String,
    pub media_type: String,
    pub size_bytes: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct IntegrationSkillDetail {
    #[serde(flatten)]
    pub skill: IntegrationSkillCatalogItem,
    pub files: Vec<IntegrationSkillSourceFile>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct IntegrationInputFile {
    pub path: String,
    #[serde(default)]
    pub media_type: String,
    pub content_base64: String,
    pub size_bytes: usize,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RunIntegrationSkillRequest {
    #[serde(default = "default_execution_mode")]
    pub mode: String,
    #[serde(default)]
    pub files: Vec<IntegrationInputFile>,
    #[serde(default)]
    pub config: Value,
}

fn default_execution_mode() -> String {
    "execute".to_string()
}

#[derive(Debug, Clone)]
pub struct IntegrationExecutionError {
    pub code: &'static str,
    pub message: String,
}

impl IntegrationExecutionError {
    fn bad_input(message: impl Into<String>) -> Self {
        Self {
            code: "INVALID_INPUT",
            message: message.into(),
        }
    }

    fn failed(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

#[derive(Debug, Clone)]
struct DecodedInputFile {
    path: String,
    bytes: Vec<u8>,
}

#[derive(Debug, Default)]
struct ParseStats {
    skipped: usize,
    tag_count: usize,
    link_count: usize,
    embed_count: usize,
}

pub fn integration_skill_catalog() -> Vec<IntegrationSkillCatalogItem> {
    vec![
        catalog_item(
            "obsidian",
            "Obsidian",
            "Markdown 知识库",
            "解析 Markdown、frontmatter、标签与双链，按相对路径增量导入本机记忆库。",
            "Vault 预检 · 幂等增量导入",
            "推荐",
            "input",
            "markdown_import",
            "folder",
            ".md,.markdown",
            true,
        ),
        catalog_item(
            "obsidian-export",
            "Obsidian 导出",
            "Markdown Vault 导出",
            "把圈选的本机记忆写成 Obsidian 笔记，直写用户选定的 Vault 目录并幂等更新。",
            "记忆圈选 · 直写 Vault",
            "推荐",
            "output",
            "vault_export",
            "memory_pick",
            "",
            true,
        ),
        catalog_item(
            "qdrant",
            "Qdrant",
            "向量数据库导出",
            "读取本地 points JSON/JSONL，保留稳定 ID 与 payload 元数据，不连接远程服务。",
            "Point 映射 · 本地迁移",
            "JSON / JSONL",
            "input",
            "record_import",
            "files",
            ".json,.jsonl",
            true,
        ),
        catalog_item(
            "milvus",
            "Milvus / Zilliz",
            "向量数据库导出",
            "解析 entity JSON、JSONL 与 CSV，保留主键和标量元数据后增量写入。",
            "Entity 映射 · 批量导入",
            "JSON / CSV",
            "input",
            "record_import",
            "files",
            ".json,.jsonl,.csv",
            true,
        ),
        catalog_item(
            "chroma-pgvector",
            "Chroma / pgvector",
            "通用向量存储",
            "识别 documents/metadatas/ids 或通用记录导出，完成可追溯的本地迁移。",
            "字段发现 · 增量导入",
            "通用",
            "input",
            "record_import",
            "files",
            ".json,.jsonl,.csv",
            true,
        ),
        catalog_item(
            "workbody",
            "Workbody",
            "办公协作上下文包",
            "从本机记忆生成可检查、可复制、可下载的最小 Markdown 上下文包。",
            "本机检索 · Markdown 交付",
            "办公",
            "output",
            "context_export",
            "query",
            "",
            false,
        ),
        catalog_item(
            "qianwen-office",
            "千问办公",
            "中文办公上下文包",
            "围绕文档主题整理本机背景、结论和证据片段，由用户确认后带入办公任务。",
            "中文材料包 · 用户确认",
            "办公",
            "output",
            "context_export",
            "query",
            "",
            false,
        ),
        catalog_item(
            "codex",
            "Codex",
            "编码 Agent",
            "把完整 memory-retrieval Skill 安装到 Codex 目录，并保留旧版本备份。",
            "真实安装 · 本机召回",
            "已内置",
            "output",
            "install_agent_skill",
            "none",
            "",
            true,
        ),
        catalog_item(
            "claude-code",
            "Claude Code",
            "编码 Agent",
            "把完整 memory-retrieval Skill 安装到 Claude Code 目录，并保留旧版本备份。",
            "真实安装 · 本机召回",
            "已内置",
            "output",
            "install_agent_skill",
            "none",
            "",
            true,
        ),
    ]
}

#[allow(clippy::too_many_arguments)]
fn catalog_item(
    id: &'static str,
    title: &'static str,
    eyebrow: &'static str,
    description: &'static str,
    capability: &'static str,
    badge: &'static str,
    direction: &'static str,
    executor: &'static str,
    input_kind: &'static str,
    accept: &'static str,
    supports_preview: bool,
) -> IntegrationSkillCatalogItem {
    IntegrationSkillCatalogItem {
        id,
        title,
        eyebrow,
        description,
        capability,
        badge,
        direction,
        executor,
        version: "1.0.0",
        input_kind,
        accept,
        supports_preview,
        file_count: source_file_contents(id).len(),
    }
}

pub fn integration_skill_detail(id: &str) -> Option<IntegrationSkillDetail> {
    let skill = integration_skill_catalog()
        .into_iter()
        .find(|item| item.id == id)?;
    let files = source_file_contents(id)
        .into_iter()
        .map(|(path, media_type, content)| IntegrationSkillSourceFile {
            path: path.to_string(),
            media_type: media_type.to_string(),
            size_bytes: content.len(),
            content: Some(content.to_string()),
        })
        .collect();
    Some(IntegrationSkillDetail { skill, files })
}

pub fn integration_skill_file(id: &str, path: &str) -> Option<(&'static str, &'static str)> {
    source_file_contents(id)
        .into_iter()
        .find(|(candidate, _, _)| candidate == &path)
        .map(|(_, media_type, content)| (media_type, content))
}

pub fn integration_skill_bundle(id: &str) -> Option<Value> {
    let detail = integration_skill_detail(id)?;
    Some(json!({
        "schemaVersion": "memorybread.integration-skill-package.v1",
        "skill": detail.skill,
        "files": detail.files,
    }))
}

pub fn run_input_summary(request: &RunIntegrationSkillRequest) -> Value {
    json!({
        "fileCount": request.files.len(),
        "totalBytes": request.files.iter().map(|file| file.size_bytes).sum::<usize>(),
        "queryLength": request.config.get("query").and_then(Value::as_str).map(|value| value.chars().count()).unwrap_or(0),
        "limit": request.config.get("limit").and_then(Value::as_u64),
        "memoryCount": request.config.get("memoryIds").and_then(Value::as_array).map(Vec::len).unwrap_or(0),
    })
}

pub fn validate_run_request(
    skill_id: &str,
    request: &RunIntegrationSkillRequest,
) -> Result<(), IntegrationExecutionError> {
    let skill = integration_skill_catalog()
        .into_iter()
        .find(|item| item.id == skill_id)
        .ok_or_else(|| IntegrationExecutionError::bad_input("未知的内置集成 Skill"))?;
    if request.mode != "preview" && request.mode != "execute" {
        return Err(IntegrationExecutionError::bad_input(
            "mode 只能是 preview 或 execute",
        ));
    }
    if request.mode == "preview" && !skill.supports_preview {
        return Err(IntegrationExecutionError::bad_input(
            "这个 Skill 不需要预检，可直接执行",
        ));
    }
    match skill.input_kind {
        "folder" | "files" if request.files.is_empty() => Err(
            IntegrationExecutionError::bad_input("请先选择需要处理的本地文件"),
        ),
        "query" => {
            let query = request
                .config
                .get("query")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim();
            if query.chars().count() < 2 {
                return Err(IntegrationExecutionError::bad_input(
                    "请输入至少 2 个字符的任务或文档线索",
                ));
            }
            Ok(())
        }
        "memory_pick" => {
            parse_memory_ids(&request.config)?;
            let vault_path = vault_path_text(&request.config);
            if request.mode == "execute"
                && (vault_path.is_empty() || !Path::new(&vault_path).is_absolute())
            {
                return Err(IntegrationExecutionError::bad_input(
                    "请选择有效的 Obsidian Vault 文件夹",
                ));
            }
            vault_subfolder(&request.config)?;
            Ok(())
        }
        _ => Ok(()),
    }
}

pub fn execute_integration_skill(
    storage: &StorageManager,
    run_id: &str,
    skill_id: &str,
    request: &RunIntegrationSkillRequest,
) -> Result<Value, IntegrationExecutionError> {
    validate_run_request(skill_id, request)?;
    storage
        .append_integration_skill_log(run_id, "info", "输入已经通过大小与路径安全检查")
        .map_err(storage_error)?;
    match skill_id {
        "obsidian" => execute_obsidian(storage, run_id, request),
        "qdrant" | "milvus" | "chroma-pgvector" => {
            execute_record_import(storage, run_id, skill_id, request)
        }
        "workbody" | "qianwen-office" => execute_context_export(storage, run_id, skill_id, request),
        "obsidian-export" => execute_vault_export(storage, run_id, request),
        "codex" | "claude-code" => execute_agent_install(storage, run_id, skill_id, request),
        _ => Err(IntegrationExecutionError::bad_input("未知的内置集成 Skill")),
    }
}

fn execute_obsidian(
    storage: &StorageManager,
    run_id: &str,
    request: &RunIntegrationSkillRequest,
) -> Result<Value, IntegrationExecutionError> {
    let files = decode_input_files(&request.files)?;
    let (items, stats) = parse_obsidian_files(&files)?;
    storage
        .append_integration_skill_log(
            run_id,
            "info",
            &format!(
                "已解析 {} 篇 Markdown，跳过 {} 个文件",
                items.len(),
                stats.skipped
            ),
        )
        .map_err(storage_error)?;
    finish_import(storage, "obsidian", &request.mode, items, stats)
}

fn execute_record_import(
    storage: &StorageManager,
    run_id: &str,
    skill_id: &str,
    request: &RunIntegrationSkillRequest,
) -> Result<Value, IntegrationExecutionError> {
    let files = decode_input_files(&request.files)?;
    let (items, stats) = parse_record_files(skill_id, &files)?;
    storage
        .append_integration_skill_log(
            run_id,
            "info",
            &format!(
                "已发现 {} 条含正文记录，跳过 {} 条",
                items.len(),
                stats.skipped
            ),
        )
        .map_err(storage_error)?;
    finish_import(storage, skill_id, &request.mode, items, stats)
}

fn finish_import(
    storage: &StorageManager,
    skill_id: &str,
    mode: &str,
    items: Vec<ImportedKnowledgeItem>,
    stats: ParseStats,
) -> Result<Value, IntegrationExecutionError> {
    let mut created = 0usize;
    let mut updated = 0usize;
    let mut unchanged = 0usize;
    let mut records = Vec::new();
    if mode == "execute" {
        for item in &items {
            let (outcome, timeline_id) = match storage
                .upsert_integration_import_item(skill_id, item)
                .map_err(storage_error)?
            {
                ImportWriteOutcome::Created(timeline_id) => {
                    created += 1;
                    ("created", timeline_id)
                }
                ImportWriteOutcome::Updated(timeline_id) => {
                    updated += 1;
                    ("updated", timeline_id)
                }
                ImportWriteOutcome::Unchanged(timeline_id) => {
                    unchanged += 1;
                    ("unchanged", timeline_id)
                }
            };
            records.push(json!({
                "id": timeline_id,
                "title": item.title,
                "path": item.source_path,
                "outcome": outcome,
            }));
        }
    }
    let sample = items
        .iter()
        .take(8)
        .map(|item| json!({"title": item.title, "path": item.source_path}))
        .collect::<Vec<_>>();
    Ok(json!({
        "kind": "import",
        "mode": mode,
        "parsed": items.len(),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": stats.skipped,
        "tagCount": stats.tag_count,
        "linkCount": stats.link_count,
        "embedCount": stats.embed_count,
        "sample": sample,
        "records": records,
    }))
}

fn execute_context_export(
    storage: &StorageManager,
    run_id: &str,
    skill_id: &str,
    request: &RunIntegrationSkillRequest,
) -> Result<Value, IntegrationExecutionError> {
    let query = request
        .config
        .get("query")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    let limit = request
        .config
        .get("limit")
        .and_then(Value::as_u64)
        .unwrap_or(8)
        .clamp(3, 20) as usize;
    let contexts = storage
        .integration_export_context(query, limit)
        .map_err(storage_error)?;
    storage
        .append_integration_skill_log(
            run_id,
            "info",
            &format!("本机检索完成，共选择 {} 条必要上下文", contexts.len()),
        )
        .map_err(storage_error)?;
    let target = if skill_id == "workbody" {
        "Workbody"
    } else {
        "千问办公"
    };
    let mut markdown = format!(
        "# {target} 任务上下文包\n\n> 由记忆面包在本机生成。请在发送到外部工具前检查内容与披露范围。\n\n## 使用线索\n\n{}\n\n## 相关记忆\n",
        query
    );
    if contexts.is_empty() {
        markdown.push_str("\n未找到直接匹配的本机记忆。请换一个更具体的项目、文档或决策线索。\n");
    }
    for (index, item) in contexts.iter().enumerate() {
        let title = item
            .get("title")
            .and_then(Value::as_str)
            .unwrap_or("未命名记忆");
        let overview = item
            .get("overview")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let content = item
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let excerpt = if overview.trim().is_empty() {
            content
        } else {
            overview
        };
        let excerpt = excerpt.chars().take(1_600).collect::<String>();
        let category = item
            .get("category")
            .and_then(Value::as_str)
            .unwrap_or("memory");
        let id = item.get("id").and_then(Value::as_i64).unwrap_or_default();
        markdown.push_str(&format!(
            "\n### {}. {}\n\n- 本机来源：{} #{}\n- 必要片段：{}\n",
            index + 1,
            title,
            category,
            id,
            excerpt
        ));
    }
    markdown.push_str("\n## 使用边界\n\n- 这些内容来自本机历史记录，不代表当前外部事实。\n- 只把当前任务需要的片段复制到外部工具。\n");
    let file_name = if skill_id == "workbody" {
        "memorybread-workbody-context.md"
    } else {
        "memorybread-qianwen-office-context.md"
    };
    Ok(json!({
        "kind": "artifact",
        "matchCount": contexts.len(),
        "records": contexts.iter().map(|item| json!({
            "id": item.get("id").cloned().unwrap_or(Value::Null),
            "title": item.get("title").cloned().unwrap_or_else(|| Value::String("未命名记忆".to_string())),
        })).collect::<Vec<_>>(),
        "artifact": {
            "fileName": file_name,
            "mediaType": "text/markdown; charset=utf-8",
            "contentBase64": BASE64_STANDARD.encode(markdown.as_bytes()),
        }
    }))
}

#[derive(Debug)]
struct VaultNotePlan {
    id: i64,
    title: String,
    file_name: String,
    content_hash: String,
    content: String,
}

fn execute_vault_export(
    storage: &StorageManager,
    run_id: &str,
    request: &RunIntegrationSkillRequest,
) -> Result<Value, IntegrationExecutionError> {
    let memory_ids = parse_memory_ids(&request.config)?;
    let subfolder = vault_subfolder(&request.config)?;
    let vault_path = vault_path_text(&request.config);
    let memories = storage
        .integration_export_memories_by_ids(&memory_ids)
        .map_err(storage_error)?;
    if memories.is_empty() {
        return Err(IntegrationExecutionError::failed(
            "MEMORY_NOT_FOUND",
            "所选记忆不存在或已被隐藏",
        ));
    }
    storage
        .append_integration_skill_log(
            run_id,
            "info",
            &format!("已选择 {} 条记忆，目标子目录 {subfolder}", memories.len()),
        )
        .map_err(storage_error)?;

    let exported_at = crate::storage::db::current_ts_ms();
    let mut used_names = HashSet::new();
    let mut plans = Vec::with_capacity(memories.len());
    for memory in &memories {
        let id = memory.get("id").and_then(Value::as_i64).unwrap_or_default();
        let title = memory
            .get("title")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or("未命名记忆")
            .chars()
            .take(240)
            .collect::<String>();
        let category = memory
            .get("category")
            .and_then(Value::as_str)
            .unwrap_or("memory");
        let observed_at = memory.get("observedAt").and_then(Value::as_i64);
        let overview = memory
            .get("overview")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let details = memory
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .chars()
            .take(MAX_EXPORT_NOTE_CHARS)
            .collect::<String>();
        let content_hash = digest_text(&format!(
            "{title}\u{1f}{category}\u{1f}{}\u{1f}{overview}\u{1f}{details}",
            observed_at.unwrap_or_default()
        ));
        let file_name = vault_note_filename(&title, id, &mut used_names);
        let content = render_vault_note(
            id,
            &title,
            category,
            observed_at,
            overview,
            &details,
            &content_hash,
            exported_at,
        );
        plans.push(VaultNotePlan {
            id,
            title,
            file_name,
            content_hash,
            content,
        });
    }

    if request.mode == "preview" {
        let target_display = if vault_path.is_empty() {
            String::new()
        } else {
            PathBuf::from(&vault_path)
                .join(&subfolder)
                .display()
                .to_string()
        };
        let overwrite_count = if vault_path.is_empty() {
            0
        } else {
            let target_dir = PathBuf::from(&vault_path).join(&subfolder);
            plans
                .iter()
                .filter(|plan| target_dir.join(&plan.file_name).exists())
                .count()
        };
        storage
            .append_integration_skill_log(
                run_id,
                "info",
                &format!("预检完成，计划写入 {} 篇笔记", plans.len()),
            )
            .map_err(storage_error)?;
        return Ok(json!({
            "kind": "vault_preview",
            "target": target_display,
            "noteCount": plans.len(),
            "overwriteCount": overwrite_count,
            "records": plans.iter().map(|plan| json!({
                "id": plan.id,
                "title": plan.title,
                "path": format!("{}/{}", subfolder, plan.file_name),
            })).collect::<Vec<_>>(),
        }));
    }

    let vault_dir = PathBuf::from(&vault_path);
    let target_dir = vault_dir.join(&subfolder);
    fs::create_dir_all(&target_dir).map_err(vault_write_error)?;
    let mut created = 0usize;
    let mut updated = 0usize;
    let mut unchanged = 0usize;
    let mut records = Vec::with_capacity(plans.len());
    for plan in &plans {
        let destination = target_dir.join(&plan.file_name);
        let relative = format!("{}/{}", subfolder, plan.file_name);
        let outcome = if destination.exists() {
            let existing = fs::read_to_string(&destination).unwrap_or_default();
            let matches = vault_note_identity(&existing).is_some_and(|(old_id, old_hash)| {
                old_id == plan.id.to_string() && old_hash == plan.content_hash
            });
            if matches {
                unchanged += 1;
                "unchanged"
            } else {
                fs::write(&destination, plan.content.as_bytes()).map_err(vault_write_error)?;
                updated += 1;
                "updated"
            }
        } else {
            fs::write(&destination, plan.content.as_bytes()).map_err(vault_write_error)?;
            created += 1;
            "created"
        };
        records.push(json!({
            "id": plan.id,
            "title": plan.title,
            "path": relative,
            "outcome": outcome,
        }));
    }
    storage
        .append_integration_skill_log(
            run_id,
            "info",
            &format!("已写入 Vault：新增 {created}，更新 {updated}，未变化 {unchanged}"),
        )
        .map_err(storage_error)?;
    Ok(json!({
        "kind": "vault_export",
        "target": target_dir.display().to_string(),
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "records": records,
    }))
}

fn parse_memory_ids(config: &Value) -> Result<Vec<i64>, IntegrationExecutionError> {
    let raw = config
        .get("memoryIds")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut ids = Vec::with_capacity(raw.len());
    for value in raw {
        let id = value.as_i64().filter(|id| *id > 0).ok_or_else(|| {
            IntegrationExecutionError::bad_input("memoryIds 只能包含正整数记忆 id")
        })?;
        if !ids.contains(&id) {
            ids.push(id);
        }
    }
    if ids.is_empty() {
        return Err(IntegrationExecutionError::bad_input(
            "请先圈选或勾选至少一条记忆",
        ));
    }
    if ids.len() > MAX_EXPORT_MEMORIES {
        return Err(IntegrationExecutionError::bad_input(format!(
            "一次最多导出 {MAX_EXPORT_MEMORIES} 条记忆"
        )));
    }
    Ok(ids)
}

fn vault_path_text(config: &Value) -> String {
    config
        .get("vaultPath")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn vault_subfolder(config: &Value) -> Result<String, IntegrationExecutionError> {
    let raw = config
        .get("subfolder")
        .and_then(Value::as_str)
        .unwrap_or("MemoryBread")
        .trim();
    let raw = if raw.is_empty() { "MemoryBread" } else { raw };
    if raw.chars().count() > 120 || raw.contains('\0') {
        return Err(IntegrationExecutionError::bad_input(
            "Vault 子目录名称过长或包含无效字符",
        ));
    }
    let normalized = raw.replace('\\', "/");
    let parts = normalized.split('/').map(str::trim).collect::<Vec<_>>();
    for part in &parts {
        if part.is_empty() || *part == "." || *part == ".." || part.starts_with('.') {
            return Err(IntegrationExecutionError::bad_input(
                "Vault 子目录包含不安全或无效的目录名",
            ));
        }
    }
    Ok(parts.join("/"))
}

fn vault_note_filename(title: &str, id: i64, used: &mut HashSet<String>) -> String {
    let cleaned = title
        .chars()
        .map(|character| match character {
            '/' | '\\' | ':' | '*' | '?' | '"' | '<' | '>' | '|' | '\0' => '-',
            other if other.is_control() => '-',
            other => other,
        })
        .collect::<String>();
    let cleaned = cleaned
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .trim_matches(|character| character == '.' || character == '-')
        .chars()
        .take(80)
        .collect::<String>();
    let base = if cleaned.is_empty() {
        format!("memory-{id}")
    } else {
        cleaned
    };
    let mut candidate = format!("{base}.md");
    if used.contains(&candidate) {
        let suffix = digest_text(&format!("{base}-{id}"));
        candidate = format!("{base}-{}.md", &suffix[..8]);
        if used.contains(&candidate) {
            candidate = format!("{base}-{id}.md");
        }
    }
    used.insert(candidate.clone());
    candidate
}

fn render_vault_note(
    id: i64,
    title: &str,
    category: &str,
    observed_at: Option<i64>,
    overview: &str,
    details: &str,
    content_hash: &str,
    exported_at: i64,
) -> String {
    let mut note = String::new();
    note.push_str("---\n");
    note.push_str(&format!("title: {}\n", yaml_scalar(title)));
    note.push_str(&format!("memorybread_id: {id}\n"));
    note.push_str(&format!("memorybread_content_hash: {content_hash}\n"));
    note.push_str(&format!("category: {}\n", yaml_scalar(category)));
    if let Some(timestamp) = observed_at {
        note.push_str(&format!("observed_at: {timestamp}\n"));
    }
    note.push_str(&format!("exported_at_ms: {exported_at}\n"));
    note.push_str("source: memorybread\n");
    note.push_str("tags:\n  - memorybread\n");
    note.push_str("---\n\n");
    note.push_str(&format!("# {title}\n\n"));
    let overview = overview.trim();
    if !overview.is_empty() {
        note.push_str(overview);
        note.push_str("\n\n");
    }
    let details = details.trim();
    if !details.is_empty() && details != overview {
        note.push_str(details);
        note.push('\n');
    }
    note
}

fn yaml_scalar(value: &str) -> String {
    let escaped = value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('\n', " ");
    format!("\"{escaped}\"")
}

fn vault_note_identity(text: &str) -> Option<(String, String)> {
    let mut lines = text.lines();
    if lines.next()?.trim() != "---" {
        return None;
    }
    let mut id = None;
    let mut hash = None;
    for line in lines {
        let trimmed = line.trim();
        if trimmed == "---" {
            break;
        }
        if let Some(rest) = trimmed.strip_prefix("memorybread_id:") {
            id = Some(rest.trim().to_string());
        } else if let Some(rest) = trimmed.strip_prefix("memorybread_content_hash:") {
            hash = Some(rest.trim().to_string());
        }
    }
    match (id, hash) {
        (Some(id), Some(hash)) => Some((id, hash)),
        _ => None,
    }
}

fn vault_write_error(error: std::io::Error) -> IntegrationExecutionError {
    IntegrationExecutionError::failed(
        "VAULT_WRITE_FAILED",
        format!("写入 Obsidian Vault 失败: {error}"),
    )
}

fn execute_agent_install(
    storage: &StorageManager,
    run_id: &str,
    skill_id: &str,
    request: &RunIntegrationSkillRequest,
) -> Result<Value, IntegrationExecutionError> {
    let home = std::env::var("HOME").map_err(|_| {
        IntegrationExecutionError::failed("HOME_UNAVAILABLE", "无法定位当前用户目录")
    })?;
    let (relative_target, package_files): (&str, Vec<(&str, &str)>) = if skill_id == "codex" {
        (
            ".agents/skills/memory-retrieval",
            vec![
                (
                    "SKILL.md",
                    include_str!("../../../integrations/codex/memory-retrieval/SKILL.md"),
                ),
                (
                    "agents/openai.yaml",
                    include_str!("../../../integrations/codex/memory-retrieval/agents/openai.yaml"),
                ),
                (
                    "scripts/recall-memory.mjs",
                    include_str!(
                        "../../../integrations/codex/memory-retrieval/scripts/recall-memory.mjs"
                    ),
                ),
            ],
        )
    } else {
        (
            ".claude/skills/memory-retrieval",
            vec![
                (
                    "SKILL.md",
                    include_str!("../../../integrations/claude-code/memory-retrieval/SKILL.md"),
                ),
                (
                    "agents/openai.yaml",
                    include_str!("../../../integrations/claude-code/memory-retrieval/agents/openai.yaml"),
                ),
                (
                    "scripts/recall-memory.mjs",
                    include_str!("../../../integrations/claude-code/memory-retrieval/scripts/recall-memory.mjs"),
                ),
            ],
        )
    };
    let target = PathBuf::from(&home).join(relative_target);
    let display_target = format!("~/{}", relative_target);
    let exists = target.exists();
    if request.mode == "preview" {
        return Ok(json!({
            "kind": "install_preview",
            "target": display_target,
            "fileCount": package_files.len(),
            "existingInstallation": exists,
            "willBackup": exists,
        }));
    }

    let staging_suffix = digest_text(run_id);
    let staging = target.with_file_name(format!(
        "memory-retrieval.installing-{}",
        &staging_suffix[..12]
    ));
    for (relative_path, content) in &package_files {
        let destination = staging.join(relative_path);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent).map_err(|error| {
                IntegrationExecutionError::failed(
                    "INSTALL_WRITE_FAILED",
                    format!("创建 Skill 目录失败: {error}"),
                )
            })?;
        }
        fs::write(&destination, content.as_bytes()).map_err(|error| {
            IntegrationExecutionError::failed(
                "INSTALL_WRITE_FAILED",
                format!("写入 Skill 文件失败: {error}"),
            )
        })?;
    }
    storage
        .append_integration_skill_log(run_id, "info", "完整 Skill 已写入本机临时安装目录")
        .map_err(storage_error)?;

    let mut backup_display = None;
    let mut backup_path = None;
    if exists {
        let suffix = crate::storage::db::current_ts_ms();
        let backup = target.with_file_name(format!("memory-retrieval.backup-{suffix}"));
        fs::rename(&target, &backup).map_err(|error| {
            IntegrationExecutionError::failed(
                "BACKUP_FAILED",
                format!("备份现有 Skill 失败: {error}"),
            )
        })?;
        backup_display = Some(format!(
            "~/{}/{}",
            relative_target
                .rsplit_once('/')
                .map(|value| value.0)
                .unwrap_or(""),
            backup
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("backup")
        ));
        backup_path = Some(backup);
        storage
            .append_integration_skill_log(run_id, "info", "现有安装已移动到带时间戳的本机备份")
            .map_err(storage_error)?;
    }
    if let Err(error) = fs::rename(&staging, &target) {
        if let Some(backup) = &backup_path {
            let _ = fs::rename(backup, &target);
        }
        return Err(IntegrationExecutionError::failed(
            "INSTALL_ACTIVATE_FAILED",
            format!("启用新 Skill 失败，已尝试恢复旧版本: {error}"),
        ));
    }
    storage
        .append_integration_skill_log(
            run_id,
            "info",
            &format!("已在本机写入 {} 个 Skill 文件", package_files.len()),
        )
        .map_err(storage_error)?;
    Ok(json!({
        "kind": "install",
        "target": display_target,
        "fileCount": package_files.len(),
        "backup": backup_display,
        "invocation": if skill_id == "codex" { "$memory-retrieval" } else { "/memory-retrieval" },
    }))
}

fn decode_input_files(
    files: &[IntegrationInputFile],
) -> Result<Vec<DecodedInputFile>, IntegrationExecutionError> {
    if files.len() > MAX_INPUT_FILES {
        return Err(IntegrationExecutionError::bad_input(format!(
            "一次最多处理 {MAX_INPUT_FILES} 个文件"
        )));
    }
    let declared_total = files.iter().map(|file| file.size_bytes).sum::<usize>();
    if declared_total > MAX_INPUT_BYTES {
        return Err(IntegrationExecutionError::bad_input(
            "本次文件总大小不能超过 24 MB",
        ));
    }
    let mut result = Vec::with_capacity(files.len());
    let mut actual_total = 0usize;
    for file in files {
        let path = normalize_relative_path(&file.path)?;
        if file.size_bytes > MAX_FILE_BYTES {
            return Err(IntegrationExecutionError::bad_input(format!(
                "单个文件不能超过 5 MB：{}",
                path
            )));
        }
        let bytes = BASE64_STANDARD.decode(&file.content_base64).map_err(|_| {
            IntegrationExecutionError::bad_input(format!("文件内容不是有效 Base64：{path}"))
        })?;
        if bytes.len() != file.size_bytes {
            return Err(IntegrationExecutionError::bad_input(format!(
                "文件大小校验失败：{}",
                path
            )));
        }
        actual_total = actual_total.saturating_add(bytes.len());
        if actual_total > MAX_INPUT_BYTES {
            return Err(IntegrationExecutionError::bad_input(
                "本次文件总大小不能超过 24 MB",
            ));
        }
        result.push(DecodedInputFile { path, bytes });
    }
    Ok(result)
}

fn normalize_relative_path(path: &str) -> Result<String, IntegrationExecutionError> {
    let normalized = path.replace('\\', "/").trim_start_matches('/').to_string();
    let parts = normalized.split('/').collect::<Vec<_>>();
    if normalized.is_empty()
        || normalized.chars().count() > 500
        || path.starts_with('/')
        || path.contains('\0')
        || parts
            .iter()
            .any(|part| part.is_empty() || *part == "." || *part == "..")
    {
        return Err(IntegrationExecutionError::bad_input(
            "文件包含不安全或无效的相对路径",
        ));
    }
    Ok(normalized)
}

fn parse_obsidian_files(
    files: &[DecodedInputFile],
) -> Result<(Vec<ImportedKnowledgeItem>, ParseStats), IntegrationExecutionError> {
    let mut items = Vec::new();
    let mut stats = ParseStats::default();
    for file in files {
        let lower = file.path.to_lowercase();
        if lower
            .split('/')
            .any(|part| part == ".obsidian" || part.starts_with('.'))
            || !(lower.ends_with(".md") || lower.ends_with(".markdown"))
        {
            stats.skipped += 1;
            continue;
        }
        let content = std::str::from_utf8(&file.bytes).map_err(|_| {
            IntegrationExecutionError::bad_input("Obsidian Markdown 必须使用 UTF-8 编码")
        })?;
        if content.trim().is_empty() {
            stats.skipped += 1;
            continue;
        }
        let tags = markdown_tags(content);
        let (links, embeds) = markdown_links(content);
        stats.tag_count += tags.len();
        stats.link_count += links.len();
        stats.embed_count += embeds.len();
        let title = markdown_filename_title(&file.path);
        let mut entities = tags
            .iter()
            .map(|value| format!("#{value}"))
            .collect::<Vec<_>>();
        entities.extend(links.iter().map(|value| format!("[[{value}]]")));
        entities.sort();
        entities.dedup();
        items.push(imported_item(
            &file.path,
            &file.path,
            &title,
            content,
            entities,
            json!({
                "sourceType": "obsidian_markdown",
                "relativePath": file.path,
                "directory": file.path.rsplit_once('/').map(|value| value.0).unwrap_or(""),
                "tags": tags,
                "links": links,
                "embeds": embeds,
            }),
        ));
    }
    Ok((items, stats))
}

fn parse_record_files(
    skill_id: &str,
    files: &[DecodedInputFile],
) -> Result<(Vec<ImportedKnowledgeItem>, ParseStats), IntegrationExecutionError> {
    let mut items = Vec::new();
    let mut stats = ParseStats::default();
    for file in files {
        let text = std::str::from_utf8(&file.bytes)
            .map_err(|_| IntegrationExecutionError::bad_input("导出文件必须使用 UTF-8 编码"))?;
        let lower = file.path.to_lowercase();
        let records = if lower.ends_with(".csv") {
            csv_records(text)?
        } else if lower.ends_with(".jsonl") {
            let mut records = Vec::new();
            for line in text.lines().map(str::trim).filter(|line| !line.is_empty()) {
                match serde_json::from_str::<Value>(line) {
                    Ok(value) => records.push(value),
                    Err(_) => stats.skipped += 1,
                }
            }
            records
        } else {
            let value = serde_json::from_str::<Value>(text).map_err(|error| {
                IntegrationExecutionError::bad_input(format!("JSON 解析失败：{error}"))
            })?;
            expand_record_container(skill_id, &value)
        };
        for (index, record) in records.iter().enumerate() {
            let Some(content) = record_text(record) else {
                stats.skipped += 1;
                continue;
            };
            if content.trim().is_empty() {
                stats.skipped += 1;
                continue;
            }
            let id = record_id(record).unwrap_or_else(|| format!("row-{}", index + 1));
            let source_key = format!("{}:{}", file.path, id);
            let title = record_title(record).unwrap_or_else(|| format!("{} · {}", skill_id, id));
            let metadata = strip_vector_fields(record);
            items.push(imported_item(
                &source_key,
                &format!("{}#{}", file.path, index + 1),
                &title,
                &content,
                Vec::new(),
                json!({
                    "sourceType": format!("{}_export", skill_id),
                    "relativePath": file.path,
                    "recordId": id,
                    "metadata": metadata,
                }),
            ));
        }
    }
    Ok((items, stats))
}

fn expand_record_container(skill_id: &str, value: &Value) -> Vec<Value> {
    if let Some(items) = value.as_array() {
        return items.clone();
    }
    if skill_id == "chroma-pgvector" {
        if let Some(documents) = value.get("documents").and_then(Value::as_array) {
            let ids = value.get("ids").and_then(Value::as_array);
            let metadatas = value.get("metadatas").and_then(Value::as_array);
            return documents
                .iter()
                .enumerate()
                .map(|(index, document)| {
                    json!({
                        "id": ids.and_then(|items| items.get(index)).cloned(),
                        "document": document,
                        "metadata": metadatas.and_then(|items| items.get(index)).cloned(),
                    })
                })
                .collect();
        }
    }
    for path in ["points", "data", "entities", "rows"] {
        if let Some(items) = value.get(path).and_then(Value::as_array) {
            return items.clone();
        }
    }
    if let Some(items) = value
        .get("result")
        .and_then(|item| item.get("points"))
        .and_then(Value::as_array)
    {
        return items.clone();
    }
    vec![value.clone()]
}

fn record_text(record: &Value) -> Option<String> {
    let candidates = [
        &["payload", "text"][..],
        &["payload", "content"],
        &["payload", "document"],
        &["metadata", "text"],
        &["metadata", "content"],
        &["page_content"],
        &["document"],
        &["text"],
        &["content"],
        &["body"],
    ];
    for path in candidates {
        if let Some(value) = value_at_path(record, path) {
            if let Some(text) = value
                .as_str()
                .map(str::trim)
                .filter(|value| !value.is_empty())
            {
                return Some(text.chars().take(MAX_IMPORTED_CONTENT_CHARS).collect());
            }
        }
    }
    None
}

fn record_id(record: &Value) -> Option<String> {
    for path in [
        &["id"][..],
        &["pk"],
        &["primary_key"],
        &["uuid"],
        &["payload", "id"],
    ] {
        if let Some(value) = value_at_path(record, path) {
            match value {
                Value::String(value) if !value.trim().is_empty() => return Some(value.clone()),
                Value::Number(value) => return Some(value.to_string()),
                _ => {}
            }
        }
    }
    None
}

fn record_title(record: &Value) -> Option<String> {
    for path in [
        &["payload", "title"][..],
        &["payload", "name"],
        &["metadata", "title"],
        &["title"],
        &["name"],
    ] {
        if let Some(value) = value_at_path(record, path).and_then(Value::as_str) {
            let title = value.trim();
            if !title.is_empty() {
                return Some(title.chars().take(240).collect());
            }
        }
    }
    None
}

fn value_at_path<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut current = value;
    for segment in path {
        current = current.get(*segment)?;
    }
    Some(current)
}

fn strip_vector_fields(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let filtered = map
                .iter()
                .filter(|(key, _)| {
                    !matches!(
                        key.to_ascii_lowercase().as_str(),
                        "vector" | "vectors" | "embedding" | "embeddings" | "values"
                    )
                })
                .map(|(key, value)| (key.clone(), strip_vector_fields(value)))
                .collect::<Map<_, _>>();
            Value::Object(filtered)
        }
        Value::Array(items) if items.len() > 256 => json!({"omittedArrayLength": items.len()}),
        Value::Array(items) => Value::Array(items.iter().map(strip_vector_fields).collect()),
        other => other.clone(),
    }
}

fn csv_records(text: &str) -> Result<Vec<Value>, IntegrationExecutionError> {
    let rows = parse_csv_rows(text);
    let Some(headers) = rows.first() else {
        return Ok(Vec::new());
    };
    if headers.is_empty() {
        return Ok(Vec::new());
    }
    Ok(rows
        .iter()
        .skip(1)
        .filter(|row| row.iter().any(|cell| !cell.trim().is_empty()))
        .map(|row| {
            let object = headers
                .iter()
                .enumerate()
                .map(|(index, header)| {
                    (
                        header.trim().to_string(),
                        Value::String(row.get(index).cloned().unwrap_or_default()),
                    )
                })
                .collect::<Map<_, _>>();
            Value::Object(object)
        })
        .collect())
}

fn parse_csv_rows(text: &str) -> Vec<Vec<String>> {
    let mut rows = Vec::new();
    let mut row = Vec::new();
    let mut field = String::new();
    let mut chars = text.chars().peekable();
    let mut quoted = false;
    while let Some(ch) = chars.next() {
        match ch {
            '"' if quoted && chars.peek() == Some(&'"') => {
                field.push('"');
                chars.next();
            }
            '"' => quoted = !quoted,
            ',' if !quoted => {
                row.push(std::mem::take(&mut field));
            }
            '\n' if !quoted => {
                row.push(std::mem::take(&mut field));
                rows.push(std::mem::take(&mut row));
            }
            '\r' if !quoted => {}
            _ => field.push(ch),
        }
    }
    if !field.is_empty() || !row.is_empty() {
        row.push(field);
        rows.push(row);
    }
    rows
}

fn imported_item(
    source_identity: &str,
    source_path: &str,
    title: &str,
    content: &str,
    entities: Vec<String>,
    metadata: Value,
) -> ImportedKnowledgeItem {
    let source_key = digest_text(source_identity);
    ImportedKnowledgeItem {
        source_key,
        source_path: source_path.to_string(),
        title: title.chars().take(240).collect(),
        content: content.chars().take(MAX_IMPORTED_CONTENT_CHARS).collect(),
        entities,
        metadata,
        content_hash: digest_text(content),
    }
}

fn digest_text(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

fn markdown_filename_title(path: &str) -> String {
    path.rsplit('/')
        .next()
        .unwrap_or(path)
        .rsplit_once('.')
        .map(|value| value.0)
        .unwrap_or(path)
        .to_string()
}

fn markdown_tags(content: &str) -> Vec<String> {
    let mut tags = Vec::new();
    let mut first_content_seen = false;
    let mut in_frontmatter = false;
    let mut collecting_tag_list = false;
    for line in content.lines() {
        let trimmed = line.trim();
        if !first_content_seen {
            if trimmed.is_empty() {
                continue;
            }
            first_content_seen = true;
            if trimmed == "---" {
                in_frontmatter = true;
                continue;
            }
        }
        if in_frontmatter && trimmed == "---" {
            in_frontmatter = false;
            collecting_tag_list = false;
            continue;
        }
        if in_frontmatter && (trimmed.starts_with("tags:") || trimmed.starts_with("tag:")) {
            let value = trimmed
                .split_once(':')
                .map(|pair| pair.1)
                .unwrap_or_default();
            collecting_tag_list = value.trim().is_empty();
            if !collecting_tag_list {
                push_markdown_tag_values(&mut tags, value);
            }
            continue;
        }
        if in_frontmatter {
            if collecting_tag_list && trimmed.starts_with('-') {
                push_markdown_tag_values(&mut tags, trimmed.trim_start_matches('-'));
            } else if trimmed.contains(':') {
                collecting_tag_list = false;
            }
            continue;
        }
        let chars = line.chars().collect::<Vec<_>>();
        let mut index = 0usize;
        while index < chars.len() {
            if chars[index] == '#'
                && (index == 0 || chars[index - 1].is_whitespace())
                && chars.get(index + 1).is_some_and(|ch| !ch.is_whitespace())
            {
                let start = index + 1;
                let mut end = start;
                while end < chars.len()
                    && !chars[end].is_whitespace()
                    && !matches!(
                        chars[end],
                        ',' | '.' | ';' | ':' | '，' | '。' | '；' | '：'
                    )
                {
                    end += 1;
                }
                if end > start {
                    tags.push(chars[start..end].iter().collect::<String>());
                }
                index = end;
            } else {
                index += 1;
            }
        }
    }
    tags.sort();
    tags.dedup();
    tags
}

fn push_markdown_tag_values(tags: &mut Vec<String>, value: &str) {
    for tag in value
        .trim()
        .trim_matches(|ch| ch == '[' || ch == ']')
        .split(',')
        .map(|value| {
            value
                .trim()
                .trim_matches(|ch| ch == '"' || ch == '\'' || ch == '#')
        })
        .filter(|value| !value.is_empty())
    {
        tags.push(tag.to_string());
    }
}

fn markdown_links(content: &str) -> (Vec<String>, Vec<String>) {
    let mut links = Vec::new();
    let mut embeds = Vec::new();
    let mut rest = content;
    while let Some(start) = rest.find("[[") {
        let before = &rest[..start];
        let tail = &rest[start + 2..];
        let Some(end) = tail.find("]]") else {
            break;
        };
        let raw = &tail[..end];
        let target = raw
            .split('|')
            .next()
            .unwrap_or(raw)
            .split('#')
            .next()
            .unwrap_or(raw)
            .trim();
        if !target.is_empty() {
            if before.ends_with('!') {
                embeds.push(target.to_string());
            } else {
                links.push(target.to_string());
            }
        }
        rest = &tail[end + 2..];
    }
    links.sort();
    links.dedup();
    embeds.sort();
    embeds.dedup();
    (links, embeds)
}

fn source_file_contents(id: &str) -> Vec<(&'static str, &'static str, &'static str)> {
    let mut common = vec![
        ("source/executor.rs", "text/x-rust", include_str!("mod.rs")),
        (
            "source/storage.rs",
            "text/x-rust",
            include_str!("../storage/repo/integration_skill.rs"),
        ),
    ];
    let specific = match id {
        "obsidian" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/builtin/obsidian/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/obsidian/integration.json"),
            ),
        ],
        "obsidian-export" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/builtin/obsidian-export/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/obsidian-export/integration.json"),
            ),
        ],
        "qdrant" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/builtin/qdrant/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/qdrant/integration.json"),
            ),
        ],
        "milvus" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/builtin/milvus/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/milvus/integration.json"),
            ),
        ],
        "chroma-pgvector" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/builtin/chroma-pgvector/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/chroma-pgvector/integration.json"),
            ),
        ],
        "workbody" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/builtin/workbody/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/workbody/integration.json"),
            ),
        ],
        "qianwen-office" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/builtin/qianwen-office/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/qianwen-office/integration.json"),
            ),
        ],
        "codex" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/codex/memory-retrieval/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/codex/integration.json"),
            ),
            (
                "agents/openai.yaml",
                "application/yaml",
                include_str!("../../../integrations/codex/memory-retrieval/agents/openai.yaml"),
            ),
            (
                "scripts/recall-memory.mjs",
                "text/javascript",
                include_str!(
                    "../../../integrations/codex/memory-retrieval/scripts/recall-memory.mjs"
                ),
            ),
        ],
        "claude-code" => vec![
            (
                "SKILL.md",
                "text/markdown",
                include_str!("../../../integrations/claude-code/memory-retrieval/SKILL.md"),
            ),
            (
                "integration.json",
                "application/json",
                include_str!("../../../integrations/builtin/claude-code/integration.json"),
            ),
            (
                "agents/openai.yaml",
                "application/yaml",
                include_str!(
                    "../../../integrations/claude-code/memory-retrieval/agents/openai.yaml"
                ),
            ),
            (
                "scripts/recall-memory.mjs",
                "text/javascript",
                include_str!(
                    "../../../integrations/claude-code/memory-retrieval/scripts/recall-memory.mjs"
                ),
            ),
        ],
        _ => return Vec::new(),
    };
    common.extend(specific);
    common
}

fn storage_error(error: crate::storage::StorageError) -> IntegrationExecutionError {
    IntegrationExecutionError::failed("STORAGE_ERROR", error.to_string())
}

pub fn list_runs(
    storage: &StorageManager,
    skill_id: Option<&str>,
    limit: usize,
) -> Result<Vec<IntegrationSkillRunRecord>, IntegrationExecutionError> {
    storage
        .list_integration_skill_runs(skill_id, limit)
        .map_err(storage_error)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_obsidian_tags_links_and_embeds() {
        let content = b"---\ntags:\n  - product\n  - memory\nowner: local\n---\n# Import plan\n\n#local [[Decision]] ![[Diagram.png]]";
        let (items, stats) = parse_obsidian_files(&[DecodedInputFile {
            path: "Projects/Plan.md".to_string(),
            bytes: content.to_vec(),
        }])
        .expect("parse");
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].title, "Plan");
        assert!(stats.tag_count >= 3);
        assert_eq!(stats.link_count, 1);
        assert_eq!(stats.embed_count, 1);
    }

    #[test]
    fn uses_obsidian_filename_instead_of_markdown_heading_as_title() {
        let content = b"# First heading\n\nDocument body.";
        let (items, _) = parse_obsidian_files(&[DecodedInputFile {
            path: "Projects/GPU.metrics.v2.markdown".to_string(),
            bytes: content.to_vec(),
        }])
        .expect("parse");

        assert_eq!(items[0].title, "GPU.metrics.v2");
    }

    #[test]
    fn parses_qdrant_and_chroma_exports_without_vectors() {
        let qdrant = json!({"points": [{"id": 7, "vector": [0.1, 0.2], "payload": {"title": "A", "text": "hello"}}]});
        let records = expand_record_container("qdrant", &qdrant);
        assert_eq!(record_text(&records[0]).as_deref(), Some("hello"));
        assert_eq!(record_id(&records[0]).as_deref(), Some("7"));
        assert!(strip_vector_fields(&records[0]).get("vector").is_none());

        let chroma = json!({"ids": ["x"], "documents": ["body"], "metadatas": [{"title": "Doc"}]});
        let records = expand_record_container("chroma-pgvector", &chroma);
        assert_eq!(record_text(&records[0]).as_deref(), Some("body"));
        assert_eq!(record_title(&records[0]).as_deref(), Some("Doc"));
    }

    #[test]
    fn catalog_exposes_real_skill_and_source_files() {
        let detail = integration_skill_detail("obsidian").expect("detail");
        assert!(detail.files.iter().any(|file| file.path == "SKILL.md"));
        assert!(detail
            .files
            .iter()
            .any(|file| file.path == "source/executor.rs"));
    }

    #[test]
    fn executes_import_and_context_export_against_local_storage() {
        let storage = StorageManager::open_in_memory().expect("storage");
        let markdown = "# Imported decision\n\nUnique integration evidence.";
        let import_request = RunIntegrationSkillRequest {
            mode: "execute".to_string(),
            files: vec![IntegrationInputFile {
                path: "Decisions/Import.md".to_string(),
                media_type: "text/markdown".to_string(),
                content_base64: BASE64_STANDARD.encode(markdown.as_bytes()),
                size_bytes: markdown.len(),
            }],
            config: json!({}),
        };
        storage
            .create_integration_skill_run(
                "import-run",
                "obsidian",
                "execute",
                &run_input_summary(&import_request),
            )
            .expect("create import run");
        let imported =
            execute_integration_skill(&storage, "import-run", "obsidian", &import_request)
                .expect("execute import");
        assert_eq!(imported.get("created").and_then(Value::as_u64), Some(1));
        assert_eq!(
            imported.pointer("/records/0/id").and_then(Value::as_i64),
            Some(1)
        );
        assert_eq!(
            imported
                .pointer("/records/0/outcome")
                .and_then(Value::as_str),
            Some("created")
        );

        let export_request = RunIntegrationSkillRequest {
            mode: "execute".to_string(),
            files: Vec::new(),
            config: json!({"query": "Unique integration", "limit": 3}),
        };
        storage
            .create_integration_skill_run(
                "export-run",
                "workbody",
                "execute",
                &run_input_summary(&export_request),
            )
            .expect("create export run");
        let exported =
            execute_integration_skill(&storage, "export-run", "workbody", &export_request)
                .expect("execute export");
        assert_eq!(exported.get("matchCount").and_then(Value::as_u64), Some(1));
        assert_eq!(
            exported.pointer("/records/0/id").and_then(Value::as_i64),
            Some(1)
        );
        let artifact = exported
            .pointer("/artifact/contentBase64")
            .and_then(Value::as_str)
            .expect("artifact");
        let decoded = BASE64_STANDARD.decode(artifact).expect("decode artifact");
        assert!(String::from_utf8(decoded)
            .expect("utf8 artifact")
            .contains("Imported decision"));
    }

    fn vault_export_request(
        mode: &str,
        memory_ids: Vec<i64>,
        vault_path: &str,
    ) -> RunIntegrationSkillRequest {
        RunIntegrationSkillRequest {
            mode: mode.to_string(),
            files: Vec::new(),
            config: json!({
                "memoryIds": memory_ids,
                "vaultPath": vault_path,
                "subfolder": "MemoryBread",
            }),
        }
    }

    #[test]
    fn validates_memory_pick_requests() {
        let empty = vault_export_request("execute", Vec::new(), "/tmp/vault");
        let error = validate_run_request("obsidian-export", &empty).unwrap_err();
        assert_eq!(error.code, "INVALID_INPUT");
        assert!(error.message.contains("至少一条记忆"));

        let too_many = vault_export_request("execute", (1..=201).collect(), "/tmp/vault");
        let error = validate_run_request("obsidian-export", &too_many).unwrap_err();
        assert!(error.message.contains("最多导出"));

        let missing_vault = vault_export_request("execute", vec![1], "");
        let error = validate_run_request("obsidian-export", &missing_vault).unwrap_err();
        assert!(error.message.contains("Vault 文件夹"));

        let relative_vault = vault_export_request("execute", vec![1], "vault/notes");
        assert!(validate_run_request("obsidian-export", &relative_vault).is_err());

        let mut traversal = vault_export_request("execute", vec![1], "/tmp/vault");
        traversal.config["subfolder"] = json!("../evil");
        let error = validate_run_request("obsidian-export", &traversal).unwrap_err();
        assert!(error.message.contains("子目录"));

        let preview_without_vault = vault_export_request("preview", vec![1], "");
        assert!(validate_run_request("obsidian-export", &preview_without_vault).is_ok());
    }

    #[test]
    fn exports_selected_memories_to_vault_idempotently() {
        let storage = StorageManager::open_in_memory().expect("storage");
        let import_markdown = |run_id: &str, path: &str, body: &str| {
            let request = RunIntegrationSkillRequest {
                mode: "execute".to_string(),
                files: vec![IntegrationInputFile {
                    path: path.to_string(),
                    media_type: "text/markdown".to_string(),
                    content_base64: BASE64_STANDARD.encode(body.as_bytes()),
                    size_bytes: body.len(),
                }],
                config: json!({}),
            };
            storage
                .create_integration_skill_run(
                    run_id,
                    "obsidian",
                    "execute",
                    &run_input_summary(&request),
                )
                .expect("create seed run");
            execute_integration_skill(&storage, run_id, "obsidian", &request).expect("seed import");
        };
        import_markdown("seed-run-1", "Notes/Alpha.md", "# Alpha\n\nAlpha evidence.");
        import_markdown("seed-run-2", "Notes/Beta.md", "# Beta\n\nBeta evidence.");

        let vault = tempfile::tempdir().expect("tempdir");
        fs::write(vault.path().join("outside.md"), "untouched").expect("outside file");
        let vault_path = vault.path().to_str().expect("vault path").to_string();

        let preview_request = vault_export_request("preview", vec![1, 2], &vault_path);
        storage
            .create_integration_skill_run(
                "vault-preview",
                "obsidian-export",
                "preview",
                &run_input_summary(&preview_request),
            )
            .expect("create preview run");
        let preview = execute_integration_skill(
            &storage,
            "vault-preview",
            "obsidian-export",
            &preview_request,
        )
        .expect("preview export");
        assert_eq!(
            preview.get("kind").and_then(Value::as_str),
            Some("vault_preview")
        );
        assert_eq!(preview.get("noteCount").and_then(Value::as_u64), Some(2));
        assert_eq!(
            preview.get("overwriteCount").and_then(Value::as_u64),
            Some(0)
        );
        let target_dir = vault.path().join("MemoryBread");
        assert!(!target_dir.exists(), "preview must not write files");

        let execute_request = vault_export_request("execute", vec![1, 2], &vault_path);
        storage
            .create_integration_skill_run(
                "vault-run-1",
                "obsidian-export",
                "execute",
                &run_input_summary(&execute_request),
            )
            .expect("create export run");
        let first =
            execute_integration_skill(&storage, "vault-run-1", "obsidian-export", &execute_request)
                .expect("first export");
        assert_eq!(
            first.get("kind").and_then(Value::as_str),
            Some("vault_export")
        );
        assert_eq!(first.get("created").and_then(Value::as_u64), Some(2));
        assert_eq!(
            first.pointer("/records/0/path").and_then(Value::as_str),
            Some("MemoryBread/Alpha.md")
        );
        let note = fs::read_to_string(target_dir.join("Alpha.md")).expect("read note");
        assert!(note.contains("memorybread_id: 1"));
        assert!(note.contains("Alpha evidence."));

        storage
            .create_integration_skill_run(
                "vault-run-2",
                "obsidian-export",
                "execute",
                &run_input_summary(&execute_request),
            )
            .expect("create second export run");
        let second =
            execute_integration_skill(&storage, "vault-run-2", "obsidian-export", &execute_request)
                .expect("second export");
        assert_eq!(second.get("created").and_then(Value::as_u64), Some(0));
        assert_eq!(second.get("unchanged").and_then(Value::as_u64), Some(2));

        import_markdown(
            "seed-run-3",
            "Notes/Alpha.md",
            "# Alpha revised\n\nRevised evidence.",
        );
        storage
            .create_integration_skill_run(
                "vault-run-3",
                "obsidian-export",
                "execute",
                &run_input_summary(&execute_request),
            )
            .expect("create third export run");
        let third =
            execute_integration_skill(&storage, "vault-run-3", "obsidian-export", &execute_request)
                .expect("third export");
        assert_eq!(third.get("updated").and_then(Value::as_u64), Some(1));
        assert_eq!(third.get("unchanged").and_then(Value::as_u64), Some(1));
        let revised = fs::read_to_string(target_dir.join("Alpha.md")).expect("read revised");
        assert!(revised.contains("Revised evidence."));
        assert_eq!(
            fs::read_to_string(vault.path().join("outside.md")).expect("outside"),
            "untouched"
        );
    }
}
