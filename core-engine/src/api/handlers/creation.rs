use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{sse::Event, IntoResponse, Response, Sse},
    Json,
};
use futures::stream::Stream;
use serde::{Deserialize, Serialize};
use std::{collections::HashMap, convert::Infallible, sync::Arc, time::Duration};
use tracing::{error, info};

use crate::api::state::AppState;

#[derive(Debug, Deserialize)]
pub struct GenerateRequest {
    pub user_prompt: String,
    #[serde(default)]
    pub design_ids: Vec<i64>,
    #[serde(default)]
    pub timeline_ids: Vec<i64>,
    #[serde(default)]
    pub capture_ids: Vec<i64>,
    #[serde(default)]
    pub doc_type: String,
    #[serde(default)]
    pub audience: String,
    #[serde(default = "default_output_format")]
    pub output_format: String,
    #[serde(default = "default_true")]
    pub inherit_format: bool,
    #[serde(default = "default_true")]
    pub enable_rag: bool,
    #[serde(default)]
    pub enable_web_search: bool,
    #[serde(default)]
    pub enable_image_generation: bool,
    #[serde(default = "default_creation_tool_ids")]
    pub enabled_tools: Vec<String>,
    #[serde(default = "default_content_weight")]
    pub content_weight: f64,
    #[serde(default = "default_quality_weight")]
    pub quality_weight: f64,
    #[serde(default = "default_completeness_weight")]
    pub completeness_weight: f64,
    #[serde(default = "default_usage_weight")]
    pub usage_weight: f64,
    #[serde(default = "default_format_weight")]
    pub format_weight: f64,
    #[serde(default = "default_freshness_weight")]
    pub freshness_weight: f64,
    #[serde(default = "default_max_references")]
    pub max_references: i64,
    #[serde(default = "default_data_search_limit")]
    pub data_search_limit: usize,
    #[serde(default)]
    pub creation_model: Option<String>,
    #[serde(default)]
    pub creation_api_key: Option<String>,
    #[serde(default)]
    pub creation_base_url: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct AgentRunRequest {
    #[serde(flatten)]
    pub generation: GenerateRequest,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub run_id: Option<String>,
    #[serde(default)]
    pub root_request: Option<String>,
    #[serde(default)]
    pub current_document: String,
    #[serde(default)]
    pub conversation: Vec<serde_json::Value>,
    #[serde(default)]
    pub selected_skills: Vec<serde_json::Value>,
    #[serde(default = "default_model_mode")]
    pub model_mode: String,
    #[serde(default)]
    pub confirmed: bool,
    #[serde(default)]
    pub resume_state: Option<serde_json::Value>,
    #[serde(default)]
    pub model_result: Option<String>,
}

#[derive(Debug, Serialize)]
struct CreationPayload {
    user_prompt: String,
    design_templates: Vec<serde_json::Value>,
    timeline_context: Option<String>,
    capture_context: Option<String>,
    doc_type: String,
    audience: String,
    output_format: String,
    inherit_format: bool,
    enable_rag: bool,
    enable_web_search: bool,
    enable_image_generation: bool,
    enabled_tools: Vec<String>,
    content_weight: f64,
    quality_weight: f64,
    completeness_weight: f64,
    usage_weight: f64,
    format_weight: f64,
    freshness_weight: f64,
    max_references: i64,
    data_search_limit: usize,
    creation_model: Option<String>,
    creation_api_key: Option<String>,
    creation_base_url: Option<String>,
}

#[derive(Debug, Serialize)]
struct AgentRunPayload {
    #[serde(flatten)]
    creation: CreationPayload,
    session_id: Option<String>,
    run_id: Option<String>,
    root_request: Option<String>,
    current_document: String,
    conversation: Vec<serde_json::Value>,
    selected_skills: Vec<serde_json::Value>,
    model_mode: String,
    confirmed: bool,
    resume_state: Option<serde_json::Value>,
    model_result: Option<String>,
}

#[derive(Debug, Serialize)]
struct ReferencePayload {
    user_prompt: String,
    doc_type: String,
    audience: String,
    inherit_format: bool,
    enable_rag: bool,
    content_weight: f64,
    quality_weight: f64,
    completeness_weight: f64,
    usage_weight: f64,
    format_weight: f64,
    freshness_weight: f64,
    max_references: i64,
}

pub async fn generate_document(
    State(state): State<Arc<AppState>>,
    Json(mut req): Json<GenerateRequest>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, (StatusCode, String)> {
    info!(
        "创作请求已接收: prompt_length={}",
        req.user_prompt.chars().count()
    );
    enrich_creation_model_from_preferences(&state, &mut req);
    let enabled_tools = normalize_creation_tool_ids(req.enabled_tools.clone());

    // 1. 查询文档模板
    let templates = state.storage.get_document_templates(Some(5)).map_err(
        |e: crate::storage::error::StorageError| {
            error!("查询文档模板失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        },
    )?;

    let design_templates: Vec<serde_json::Value> = templates
        .into_iter()
        .map(|t| {
            serde_json::json!({
                "title": t.title,
                "doc_type": t.doc_type,
                "sections_json": t.sections_json,
                "style_phrases": t.style_phrases,
            })
        })
        .collect();

    // 2. 构建时间线上下文（简化版）
    let timeline_context = if !req.timeline_ids.is_empty() {
        Some(format!("时间线 IDs: {:?}", req.timeline_ids))
    } else {
        None
    };

    // 3. 构建采集记录上下文（简化版）
    let capture_context = if !req.capture_ids.is_empty() {
        Some(format!("采集记录 IDs: {:?}", req.capture_ids))
    } else {
        None
    };

    // 4. 调用 ai-sidecar creation 服务
    let payload = CreationPayload {
        user_prompt: req.user_prompt,
        design_templates,
        timeline_context,
        capture_context,
        doc_type: req.doc_type,
        audience: req.audience,
        output_format: req.output_format,
        inherit_format: req.inherit_format,
        enable_rag: req.enable_rag,
        enable_web_search: req.enable_web_search,
        enable_image_generation: req.enable_image_generation,
        enabled_tools,
        content_weight: req.content_weight,
        quality_weight: req.quality_weight,
        completeness_weight: req.completeness_weight,
        usage_weight: req.usage_weight,
        format_weight: req.format_weight,
        freshness_weight: req.freshness_weight,
        max_references: req.max_references.clamp(1, 30),
        data_search_limit: req.data_search_limit.clamp(1, 50),
        creation_model: req.creation_model,
        creation_api_key: req.creation_api_key,
        creation_base_url: req.creation_base_url,
    };

    let client = reqwest::Client::new();
    let response = client
        .post("http://127.0.0.1:8001/creation/generate")
        .json(&payload)
        .send()
        .await
        .map_err(|e| {
            error!("调用 ai-sidecar 失败: {}", e);
            (StatusCode::BAD_GATEWAY, format!("AI 服务不可用: {}", e))
        })?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        error!("ai-sidecar 返回错误: {} - {}", status, body);
        return Err((StatusCode::BAD_GATEWAY, format!("AI 服务错误: {}", body)));
    }

    // 5. 转发 SSE 流
    let stream = async_stream::stream! {
        let mut bytes_stream = response.bytes_stream();
        use futures::StreamExt;
        let mut buffer = Vec::new();

        while let Some(chunk) = bytes_stream.next().await {
            match chunk {
                Ok(bytes) => {
                    for content in append_sse_chunk(&mut buffer, &bytes) {
                        yield Ok(Event::default().data(content));
                    }
                }
                Err(e) => {
                    error!("流式读取错误: {}", e);
                    let payload = serde_json::json!({ "error": format!("AI 流式响应中断: {}", e) }).to_string();
                    yield Ok(Event::default().data(payload));
                    break;
                }
            }
        }
        if let Some(content) = take_sse_tail(&mut buffer) {
            yield Ok(Event::default().data(content));
        }
    };

    Ok(Sse::new(stream).keep_alive(
        axum::response::sse::KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("keep-alive"),
    ))
}

pub async fn run_creation_agent(
    State(state): State<Arc<AppState>>,
    Json(mut req): Json<AgentRunRequest>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, (StatusCode, String)> {
    if !matches!(req.model_mode.as_str(), "local" | "external") {
        return Err((
            StatusCode::BAD_REQUEST,
            "model_mode 只支持 local 或 external".to_string(),
        ));
    }
    info!(
        "创作 Agent 请求已接收: mode={}, prompt_length={}",
        req.model_mode,
        req.generation.user_prompt.chars().count()
    );
    if req.model_mode == "local" {
        enrich_creation_model_from_preferences(&state, &mut req.generation);
    } else {
        req.generation.creation_model = None;
        req.generation.creation_api_key = None;
        req.generation.creation_base_url = None;
    }

    let stored_context = if let Some(session_id) = req.session_id.as_deref() {
        state
            .storage
            .with_conn(|conn| {
                crate::storage::repo::creation_history::get_session_context(conn, session_id)
                    .map_err(Into::into)
            })
            .map_err(|e| {
                error!("恢复创作会话上下文失败: {}", e);
                (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
            })?
    } else {
        None
    };
    if let Some(context) = stored_context {
        if req.current_document.trim().is_empty() {
            req.current_document = context.latest.generated_content.clone();
        }
        if req
            .root_request
            .as_deref()
            .map(str::trim)
            .unwrap_or_default()
            .is_empty()
        {
            req.root_request = Some(context.root_request.clone());
        }
        let stored_conversation = context
            .latest
            .conversation_json
            .as_deref()
            .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
            .unwrap_or_default();
        req.conversation = merge_creation_conversation(
            stored_conversation,
            req.conversation,
            req.root_request.as_deref(),
        );
    }
    if req
        .root_request
        .as_deref()
        .map(str::trim)
        .unwrap_or_default()
        .is_empty()
    {
        req.root_request = conversation_root_request(&req.conversation)
            .or_else(|| Some(req.generation.user_prompt.clone()));
    }

    let templates = state.storage.get_document_templates(Some(5)).map_err(|e| {
        error!("查询文档模板失败: {}", e);
        (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
    })?;
    let design_templates = templates
        .into_iter()
        .map(|item| {
            serde_json::json!({
                "title": item.title,
                "doc_type": item.doc_type,
                "sections_json": item.sections_json,
                "style_phrases": item.style_phrases,
            })
        })
        .collect();
    let timeline_context = (!req.generation.timeline_ids.is_empty())
        .then(|| format!("时间线 IDs: {:?}", req.generation.timeline_ids));
    let capture_context = (!req.generation.capture_ids.is_empty())
        .then(|| format!("采集记录 IDs: {:?}", req.generation.capture_ids));
    let generation = req.generation;
    let enabled_tools = normalize_creation_tool_ids(generation.enabled_tools.clone());
    let payload = AgentRunPayload {
        creation: CreationPayload {
            user_prompt: generation.user_prompt,
            design_templates,
            timeline_context,
            capture_context,
            doc_type: generation.doc_type,
            audience: generation.audience,
            output_format: generation.output_format,
            inherit_format: generation.inherit_format,
            enable_rag: generation.enable_rag,
            enable_web_search: generation.enable_web_search,
            enable_image_generation: generation.enable_image_generation,
            enabled_tools,
            content_weight: generation.content_weight,
            quality_weight: generation.quality_weight,
            completeness_weight: generation.completeness_weight,
            usage_weight: generation.usage_weight,
            format_weight: generation.format_weight,
            freshness_weight: generation.freshness_weight,
            max_references: generation.max_references.clamp(1, 30),
            data_search_limit: generation.data_search_limit.clamp(1, 50),
            creation_model: generation.creation_model,
            creation_api_key: generation.creation_api_key,
            creation_base_url: generation.creation_base_url,
        },
        session_id: req.session_id,
        run_id: req.run_id,
        root_request: req.root_request,
        current_document: req.current_document,
        conversation: req.conversation,
        selected_skills: req.selected_skills,
        model_mode: req.model_mode,
        confirmed: req.confirmed,
        resume_state: req.resume_state,
        model_result: req.model_result,
    };

    let response = reqwest::Client::new()
        .post("http://127.0.0.1:8001/creation/agent/run")
        .json(&payload)
        .send()
        .await
        .map_err(|e| {
            error!("调用创作 Agent sidecar 失败: {}", e);
            (
                StatusCode::BAD_GATEWAY,
                format!("创作 Agent 服务不可用: {}", e),
            )
        })?;
    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        error!("创作 Agent sidecar 返回错误: {} - {}", status, body);
        return Err((
            StatusCode::BAD_GATEWAY,
            format!("创作 Agent 服务错误: {}", body),
        ));
    }

    let stream = async_stream::stream! {
        let mut bytes_stream = response.bytes_stream();
        use futures::StreamExt;
        let mut buffer = Vec::new();
        while let Some(chunk) = bytes_stream.next().await {
            match chunk {
                Ok(bytes) => {
                    for content in append_sse_chunk(&mut buffer, &bytes) {
                        yield Ok(Event::default().data(content));
                    }
                }
                Err(e) => {
                    error!("创作 Agent 流式读取错误: {}", e);
                    let payload = serde_json::json!({
                        "schema_version": "creation.agent.v1",
                        "type": "run.failed",
                        "status": "failed",
                        "summary": format!("创作 Agent 流式响应中断: {}", e)
                    }).to_string();
                    yield Ok(Event::default().data(payload));
                    break;
                }
            }
        }
        if let Some(content) = take_sse_tail(&mut buffer) {
            yield Ok(Event::default().data(content));
        }
    };

    Ok(Sse::new(stream).keep_alive(
        axum::response::sse::KeepAlive::new()
            .interval(Duration::from_secs(15))
            .text("keep-alive"),
    ))
}

pub async fn preview_references(
    Json(req): Json<GenerateRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let payload = ReferencePayload {
        user_prompt: req.user_prompt,
        doc_type: req.doc_type,
        audience: req.audience,
        inherit_format: req.inherit_format,
        enable_rag: req.enable_rag,
        content_weight: req.content_weight,
        quality_weight: req.quality_weight,
        completeness_weight: req.completeness_weight,
        usage_weight: req.usage_weight,
        format_weight: req.format_weight,
        freshness_weight: req.freshness_weight,
        max_references: req.max_references,
    };

    let client = reqwest::Client::new();
    let response = client
        .post("http://127.0.0.1:8001/creation/references")
        .json(&payload)
        .send()
        .await
        .map_err(|e| {
            error!("调用 ai-sidecar 参考资料预览失败: {}", e);
            (StatusCode::BAD_GATEWAY, format!("AI 服务不可用: {}", e))
        })?;

    if !response.status().is_success() {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        error!("ai-sidecar 参考资料预览返回错误: {} - {}", status, body);
        return Err((StatusCode::BAD_GATEWAY, format!("AI 服务错误: {}", body)));
    }

    let body = response.json::<serde_json::Value>().await.map_err(|e| {
        error!("解析参考资料预览响应失败: {}", e);
        (
            StatusCode::BAD_GATEWAY,
            format!("AI 服务响应格式错误: {}", e),
        )
    })?;

    Ok(Json(body))
}

fn default_true() -> bool {
    true
}

fn default_creation_tool_ids() -> Vec<String> {
    vec![
        "internet_search".to_string(),
        "memory_search".to_string(),
        "data_search".to_string(),
        "webpage_scrape".to_string(),
    ]
}

fn normalize_creation_tool_ids(tool_ids: Vec<String>) -> Vec<String> {
    let mut normalized = default_creation_tool_ids();
    for tool_id in tool_ids {
        let tool_id = tool_id.trim();
        if tool_id.is_empty() || normalized.iter().any(|item| item == tool_id) {
            continue;
        }
        normalized.push(tool_id.to_string());
    }
    normalized
}

fn default_output_format() -> String {
    "markdown".to_string()
}

fn default_content_weight() -> f64 {
    0.45
}

fn default_quality_weight() -> f64 {
    0.15
}

fn default_completeness_weight() -> f64 {
    0.15
}

fn default_usage_weight() -> f64 {
    0.10
}

fn default_format_weight() -> f64 {
    0.10
}

fn default_freshness_weight() -> f64 {
    0.05
}

fn default_max_references() -> i64 {
    10
}

fn default_data_search_limit() -> usize {
    30
}

fn default_model_mode() -> String {
    "local".to_string()
}

fn append_sse_chunk(buffer: &mut Vec<u8>, chunk: &[u8]) -> Vec<String> {
    buffer.extend_from_slice(chunk);
    let mut events = Vec::new();
    while let Some(newline_index) = buffer.iter().position(|byte| *byte == b'\n') {
        let mut line = buffer.drain(..=newline_index).collect::<Vec<_>>();
        line.pop();
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        if let Some(content) = line.strip_prefix(b"data: ") {
            events.push(String::from_utf8_lossy(content).into_owned());
        }
    }
    events
}

fn take_sse_tail(buffer: &mut Vec<u8>) -> Option<String> {
    while buffer
        .last()
        .is_some_and(|byte| matches!(byte, b'\r' | b'\n' | b' ' | b'\t'))
    {
        buffer.pop();
    }
    buffer
        .strip_prefix(b"data: ")
        .map(|content| String::from_utf8_lossy(content).into_owned())
}

fn normalize_conversation_item(value: &serde_json::Value) -> Option<serde_json::Value> {
    let role = value.get("role")?.as_str()?.trim();
    let content = value.get("content")?.as_str()?.trim();
    if !matches!(role, "user" | "assistant") || content.is_empty() {
        return None;
    }
    Some(serde_json::json!({
        "role": role,
        "content": content.chars().take(12_000).collect::<String>(),
    }))
}

fn merge_creation_conversation(
    stored: Vec<serde_json::Value>,
    current: Vec<serde_json::Value>,
    root_request: Option<&str>,
) -> Vec<serde_json::Value> {
    let mut merged = Vec::new();
    let mut stored_counts = HashMap::<String, usize>::new();
    let normalized_root = root_request
        .map(str::trim)
        .filter(|value| !value.is_empty());
    if let Some(root) = normalized_root {
        let item = serde_json::json!({"role": "user", "content": root});
        let key = format!("user\u{1f}{}", root);
        *stored_counts.entry(key).or_default() += 1;
        merged.push(item);
    }
    for value in stored {
        let Some(item) = normalize_conversation_item(&value) else {
            continue;
        };
        let role = item["role"].as_str().unwrap_or_default();
        let content = item["content"].as_str().unwrap_or_default();
        let key = format!("{role}\u{1f}{content}");
        if role == "user"
            && Some(content) == normalized_root
            && stored_counts.get(&key).copied().unwrap_or_default() > 0
        {
            continue;
        }
        *stored_counts.entry(key).or_default() += 1;
        merged.push(item);
    }
    let mut current_counts = HashMap::<String, usize>::new();
    for value in current {
        let Some(item) = normalize_conversation_item(&value) else {
            continue;
        };
        let role = item["role"].as_str().unwrap_or_default();
        let content = item["content"].as_str().unwrap_or_default();
        let key = format!("{role}\u{1f}{content}");
        let occurrence = current_counts.entry(key.clone()).or_default();
        *occurrence += 1;
        if *occurrence <= stored_counts.get(&key).copied().unwrap_or_default() {
            continue;
        }
        merged.push(item);
    }
    if merged.len() <= 60 {
        return merged;
    }
    let tail = merged.split_off(merged.len() - 56);
    merged.truncate(4);
    merged.extend(tail);
    merged
}

fn normalize_history_conversation_item(value: &serde_json::Value) -> Option<serde_json::Value> {
    let normalized = normalize_conversation_item(value)?;
    let mut object = value.as_object().cloned().unwrap_or_default();
    object.insert("role".to_string(), normalized["role"].clone());
    object.insert("content".to_string(), normalized["content"].clone());
    Some(serde_json::Value::Object(object))
}

fn merge_creation_history_conversation(
    stored: Vec<serde_json::Value>,
    current: Vec<serde_json::Value>,
    root_request: Option<&str>,
) -> Vec<serde_json::Value> {
    let mut merged = Vec::new();
    let mut stored_counts = HashMap::<String, usize>::new();
    for value in stored {
        let Some(item) = normalize_history_conversation_item(&value) else {
            continue;
        };
        let role = item["role"].as_str().unwrap_or_default();
        let content = item["content"].as_str().unwrap_or_default();
        *stored_counts
            .entry(format!("{role}\u{1f}{content}"))
            .or_default() += 1;
        merged.push(item);
    }

    let mut current_counts = HashMap::<String, usize>::new();
    for value in current {
        let Some(item) = normalize_history_conversation_item(&value) else {
            continue;
        };
        let role = item["role"].as_str().unwrap_or_default();
        let content = item["content"].as_str().unwrap_or_default();
        let key = format!("{role}\u{1f}{content}");
        let occurrence = current_counts.entry(key.clone()).or_default();
        *occurrence += 1;
        if *occurrence <= stored_counts.get(&key).copied().unwrap_or_default() {
            continue;
        }
        merged.push(item);
    }

    if let Some(root) = root_request
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        let has_root = merged.iter().any(|item| {
            item["role"].as_str() == Some("user") && item["content"].as_str() == Some(root)
        });
        if !has_root {
            merged.insert(0, serde_json::json!({"role": "user", "content": root}));
        }
    }

    if merged.len() <= 60 {
        return merged;
    }
    let tail = merged.split_off(merged.len() - 56);
    merged.truncate(4);
    merged.extend(tail);
    merged
}

fn user_instruction_count(conversation: &[serde_json::Value], instruction: &str) -> usize {
    let instruction = instruction.trim();
    conversation
        .iter()
        .filter(|value| {
            normalize_conversation_item(value).is_some_and(|item| {
                item["role"].as_str() == Some("user")
                    && item["content"].as_str() == Some(instruction)
            })
        })
        .count()
}

fn ensure_current_creation_instruction(
    conversation: &mut Vec<serde_json::Value>,
    stored: &[serde_json::Value],
    instruction: &str,
) {
    let instruction = instruction.trim();
    if instruction.is_empty()
        || user_instruction_count(conversation, instruction)
            > user_instruction_count(stored, instruction)
    {
        return;
    }

    let insert_at = conversation
        .iter()
        .rposition(|value| {
            normalize_conversation_item(value)
                .is_some_and(|item| item["role"].as_str() == Some("assistant"))
        })
        .unwrap_or(conversation.len());
    let created_at = chrono::Utc::now().timestamp_millis();
    conversation.insert(
        insert_at,
        serde_json::json!({
            "id": format!("server-user-{created_at}"),
            "role": "user",
            "content": instruction.chars().take(12_000).collect::<String>(),
            "createdAt": created_at,
        }),
    );
}

fn conversation_root_request(conversation: &[serde_json::Value]) -> Option<String> {
    conversation.iter().find_map(|value| {
        let normalized = normalize_conversation_item(value)?;
        (normalized["role"].as_str() == Some("user")).then(|| {
            normalized["content"]
                .as_str()
                .unwrap_or_default()
                .to_string()
        })
    })
}

fn normalize_edit_operation(value: &str) -> &str {
    match value {
        "create_document" | "append_section" | "replace_section" | "delete_section"
        | "rewrite_document" | "revise_document" => value,
        _ => "rewrite_document",
    }
}

fn creation_model_name(id: &str) -> &str {
    match id {
        "mbcd-plus-v1" => "claude-opus-4-8",
        "mbcd-std-v1" => "qwen3.5:4b",
        "claude-opus-4-8" => "claude-opus-4-8",
        "qwen-3-5-4b" => "qwen3.5:4b",
        _ => id,
    }
}

fn enrich_creation_model_from_preferences(state: &Arc<AppState>, req: &mut GenerateRequest) {
    if req.creation_model.is_some() {
        return;
    }

    let Some(raw) = state
        .storage
        .get_preference_value("creation.models")
        .ok()
        .flatten()
    else {
        return;
    };
    let Ok(models) = serde_json::from_str::<serde_json::Value>(&raw) else {
        return;
    };
    let Some(items) = models.as_array() else {
        return;
    };
    let Some(selected) = items.iter().find(|item| {
        item.get("enabled")
            .and_then(|value| value.as_bool())
            .unwrap_or(false)
    }) else {
        return;
    };
    let Some(id) = selected.get("id").and_then(|value| value.as_str()) else {
        return;
    };
    let api_key = selected
        .get("apiKey")
        .and_then(|value| value.as_str())
        .filter(|value| !value.trim().is_empty())
        .map(|value| value.to_string());

    if id != "mbcd-std-v1" && api_key.is_none() {
        return;
    }

    req.creation_model = Some(creation_model_name(id).to_string());
    req.creation_api_key = api_key;
    req.creation_base_url = selected
        .get("baseUrl")
        .and_then(|value| value.as_str())
        .filter(|value| !value.trim().is_empty())
        .map(|value| value.to_string());
}

#[derive(Debug, Deserialize)]
pub struct SaveHistoryRequest {
    pub prompt: String,
    pub generated_content: String,
    pub doc_type: Option<String>,
    pub audience: Option<String>,
    pub reference_count: i64,
    #[serde(default)]
    pub references: Vec<serde_json::Value>,
    pub model: Option<String>,
    #[serde(default)]
    pub latency_ms: Option<i64>,
    #[serde(default)]
    pub session_id: Option<String>,
    #[serde(default)]
    pub history_id: Option<i64>,
    #[serde(default)]
    pub conversation: Vec<serde_json::Value>,
    #[serde(default)]
    pub agent_trace: Vec<serde_json::Value>,
    #[serde(default)]
    pub goal: Option<serde_json::Value>,
    #[serde(default)]
    pub root_request: Option<String>,
    #[serde(default)]
    pub edit_operation: Option<String>,
    #[serde(default)]
    pub document_patch: Option<serde_json::Value>,
    #[serde(default)]
    pub evidence: Vec<serde_json::Value>,
}

#[derive(Debug, Serialize)]
pub struct SaveHistoryResponse {
    pub id: i64,
}

pub async fn save_history(
    State(state): State<Arc<AppState>>,
    Json(req): Json<SaveHistoryRequest>,
) -> Result<Json<SaveHistoryResponse>, (StatusCode, String)> {
    let id = state
        .storage
        .with_conn(|conn| {
            let references_json = serde_json::to_string(&req.references)?;
            let evidence_ids = req
                .evidence
                .iter()
                .filter(|item| {
                    item.get("validation_status")
                        .and_then(|value| value.as_str())
                        == Some("verified")
                })
                .filter_map(|item| item.get("id").and_then(|value| value.as_str()))
                .map(ToOwned::to_owned)
                .collect::<Vec<_>>();
            let agent_trace_json = serde_json::to_string(&req.agent_trace)?;
            let goal_json = req.goal.as_ref().map(serde_json::to_string).transpose()?;
            let session_id = req
                .session_id
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty());
            let session_prior = session_id
                .map(|session_id| {
                    crate::storage::repo::creation_history::get_session_context(conn, session_id)
                })
                .transpose()?
                .flatten();
            let resumed_existing_session = session_prior.is_some();
            let prior = if session_prior.is_some() {
                session_prior
            } else {
                req.history_id
                    .map(|history_id| {
                        crate::storage::repo::creation_history::get_by_id(conn, history_id)
                    })
                    .transpose()?
                    .flatten()
                    .filter(|latest| {
                        latest
                            .session_id
                            .as_deref()
                            .map(str::trim)
                            .filter(|value| !value.is_empty())
                            .is_none()
                    })
                    .map(|latest| {
                        let root_request = latest
                            .root_request
                            .as_deref()
                            .map(str::trim)
                            .filter(|value| !value.is_empty())
                            .map(ToOwned::to_owned)
                            .unwrap_or_else(|| latest.prompt.clone());
                        crate::storage::repo::creation_history::CreationSessionContext {
                            root_request,
                            latest,
                        }
                    })
            };
            let mut merged_evidence = prior
                .as_ref()
                .and_then(|context| context.latest.evidence_json.as_deref())
                .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
                .unwrap_or_default();
            for evidence in &req.evidence {
                let evidence_id = evidence.get("id").and_then(|value| value.as_str());
                if let Some(index) = evidence_id.and_then(|id| {
                    merged_evidence.iter().position(|existing| {
                        existing.get("id").and_then(|value| value.as_str()) == Some(id)
                    })
                }) {
                    merged_evidence[index] = evidence.clone();
                } else {
                    merged_evidence.push(evidence.clone());
                }
            }
            let evidence_json = serde_json::to_string(&merged_evidence)?;
            let root_request = req
                .root_request
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(ToOwned::to_owned)
                .or_else(|| prior.as_ref().map(|context| context.root_request.clone()))
                .or_else(|| conversation_root_request(&req.conversation))
                .unwrap_or_else(|| req.prompt.clone());
            let stored_conversation = prior
                .as_ref()
                .and_then(|context| context.latest.conversation_json.as_deref())
                .and_then(|value| serde_json::from_str::<Vec<serde_json::Value>>(value).ok())
                .unwrap_or_default();
            let mut merged_conversation = merge_creation_history_conversation(
                stored_conversation.clone(),
                req.conversation.clone(),
                Some(&root_request),
            );
            ensure_current_creation_instruction(
                &mut merged_conversation,
                &stored_conversation,
                &req.prompt,
            );
            let conversation_json = serde_json::to_string(&merged_conversation)?;
            let revision_no = if resumed_existing_session {
                crate::storage::repo::creation_history::next_revision_no(
                    conn,
                    session_id.unwrap_or_default(),
                )?
            } else {
                prior
                    .as_ref()
                    .map(|context| context.latest.revision_no + 1)
                    .unwrap_or(1)
            };
            let edit_operation = normalize_edit_operation(req.edit_operation.as_deref().unwrap_or(
                if prior.is_some() {
                    "rewrite_document"
                } else {
                    "create_document"
                },
            ));
            let document_patch_json = req
                .document_patch
                .as_ref()
                .map(serde_json::to_string)
                .transpose()?;
            let history_id = if let (Some(context), Some(session_id)) = (prior.as_ref(), session_id)
            {
                crate::storage::repo::creation_history::update_session(
                    conn,
                    context.latest.id,
                    &req.prompt,
                    &req.generated_content,
                    req.doc_type.as_deref(),
                    req.audience.as_deref(),
                    req.reference_count,
                    Some(&references_json),
                    req.model.as_deref(),
                    req.latency_ms,
                    session_id,
                    Some(&conversation_json),
                    Some(&agent_trace_json),
                    goal_json.as_deref(),
                    &root_request,
                    revision_no,
                    edit_operation,
                    document_patch_json.as_deref(),
                )?;
                context.latest.id
            } else {
                crate::storage::repo::creation_history::insert(
                    conn,
                    &req.prompt,
                    &req.generated_content,
                    req.doc_type.as_deref(),
                    req.audience.as_deref(),
                    req.reference_count,
                    Some(&references_json),
                    req.model.as_deref(),
                    req.latency_ms,
                    session_id,
                    Some(&conversation_json),
                    Some(&agent_trace_json),
                    goal_json.as_deref(),
                    Some(&root_request),
                    None,
                    revision_no,
                    edit_operation,
                    document_patch_json.as_deref(),
                )?
            };
            crate::storage::repo::creation_history::set_evidence_json(
                conn,
                history_id,
                &evidence_json,
            )?;
            crate::storage::repo::creation_evidence::attach_to_history(
                conn,
                history_id,
                &evidence_ids,
            )?;
            Ok(history_id)
        })
        .map_err(|e| {
            error!("保存创作记录失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        })?;

    Ok(Json(SaveHistoryResponse { id }))
}

#[derive(Debug, Deserialize)]
pub struct ListHistoryParams {
    #[serde(default)]
    pub limit: Option<usize>,
    #[serde(default)]
    pub offset: usize,
    #[serde(default)]
    pub q: Option<String>,
    #[serde(default)]
    pub paged: bool,
}

#[derive(Debug, Serialize)]
pub struct HistoryPageResponse {
    pub items: Vec<crate::storage::repo::creation_history::CreationHistory>,
    pub total: usize,
    pub limit: usize,
    pub offset: usize,
}

pub async fn list_history(
    State(state): State<Arc<AppState>>,
    Query(params): Query<ListHistoryParams>,
) -> Result<Response, (StatusCode, String)> {
    let limit = params
        .limit
        .unwrap_or(if params.paged { 20 } else { 50 })
        .clamp(1, 100);
    let offset = params.offset.min(1_000_000);
    let query = params
        .q
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    let (histories, total) = state
        .storage
        .with_conn(|conn| {
            crate::storage::repo::creation_history::list_page(conn, query.as_deref(), limit, offset)
                .map_err(Into::into)
        })
        .map_err(|e| {
            error!("查询创作记录失败: {}", e);
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
        })?;

    if params.paged {
        Ok(Json(HistoryPageResponse {
            items: histories,
            total,
            limit,
            offset,
        })
        .into_response())
    } else {
        // 保留旧客户端依赖的数组响应；新页面显式传 paged=true 获取分页元数据。
        Ok(Json(histories).into_response())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merges_persisted_root_with_latest_conversation_without_duplicates() {
        let stored = vec![
            serde_json::json!({"role": "user", "content": "生成新能源行业方案", "id": "old-1"}),
            serde_json::json!({"role": "assistant", "content": "已生成首版"}),
        ];
        let current = vec![
            serde_json::json!({"role": "assistant", "content": "已生成首版"}),
            serde_json::json!({"role": "user", "content": "补充行业调研"}),
        ];

        let merged = merge_creation_conversation(stored, current, Some("生成新能源行业方案"));

        assert_eq!(merged.len(), 3);
        assert_eq!(
            conversation_root_request(&merged).as_deref(),
            Some("生成新能源行业方案")
        );
        assert_eq!(merged[2]["content"], "补充行业调研");
        assert!(merged.iter().all(|item| item.get("id").is_none()));
    }

    #[test]
    fn keeps_root_and_recent_turns_when_conversation_is_large() {
        let current = (0..80)
            .map(|index| {
                serde_json::json!({
                    "role": if index % 2 == 0 { "user" } else { "assistant" },
                    "content": format!("第 {index} 条")
                })
            })
            .collect();
        let merged = merge_creation_conversation(Vec::new(), current, Some("最初的完整需求"));

        assert_eq!(merged.len(), 60);
        assert_eq!(merged[0]["content"], "最初的完整需求");
        assert_eq!(merged.last().unwrap()["content"], "第 79 条");
    }

    #[test]
    fn preserves_a_repeated_instruction_when_it_is_a_new_occurrence() {
        let stored = vec![
            serde_json::json!({"role": "user", "content": "生成方案"}),
            serde_json::json!({"role": "user", "content": "继续完善"}),
        ];
        let current = vec![
            serde_json::json!({"role": "user", "content": "生成方案"}),
            serde_json::json!({"role": "user", "content": "继续完善"}),
            serde_json::json!({"role": "user", "content": "继续完善"}),
        ];

        let merged = merge_creation_conversation(stored, current, Some("生成方案"));
        let repeated = merged
            .iter()
            .filter(|item| item["content"] == "继续完善")
            .count();

        assert_eq!(repeated, 2);
    }

    #[test]
    fn history_merge_preserves_message_metadata_and_restores_a_missing_turn() {
        let stored = vec![
            serde_json::json!({
                "id": "user-1",
                "role": "user",
                "content": "写一份周年员工的礼物指南",
                "runIds": ["run-1"]
            }),
            serde_json::json!({
                "id": "assistant-1",
                "role": "assistant",
                "content": "文档已更新",
                "runId": "run-1"
            }),
        ];
        let current = vec![
            stored[0].clone(),
            stored[1].clone(),
            serde_json::json!({
                "id": "assistant-2",
                "role": "assistant",
                "content": "已完成 72 处调整",
                "runId": "run-2"
            }),
        ];

        let mut merged = merge_creation_history_conversation(
            stored.clone(),
            current,
            Some("写一份周年员工的礼物指南"),
        );
        ensure_current_creation_instruction(&mut merged, &stored, "参考快手员工周年礼物方案");

        assert_eq!(merged[0]["id"], "user-1");
        assert_eq!(merged[0]["runIds"][0], "run-1");
        assert_eq!(merged[2]["content"], "参考快手员工周年礼物方案");
        assert!(merged[2]["id"]
            .as_str()
            .is_some_and(|value| value.starts_with("server-user-")));
        assert_eq!(merged[3]["runId"], "run-2");
    }

    #[test]
    fn history_merge_keeps_repeated_current_instruction_occurrences() {
        let stored = vec![
            serde_json::json!({"role": "user", "content": "生成方案"}),
            serde_json::json!({"role": "user", "content": "继续完善"}),
        ];
        let mut merged =
            merge_creation_history_conversation(stored.clone(), stored.clone(), Some("生成方案"));

        ensure_current_creation_instruction(&mut merged, &stored, "继续完善");

        assert_eq!(user_instruction_count(&merged, "继续完善"), 2);
    }

    #[test]
    fn preserves_multi_section_revision_operation_for_history() {
        assert_eq!(
            normalize_edit_operation("revise_document"),
            "revise_document"
        );
    }

    #[test]
    fn relays_sse_when_a_chinese_character_crosses_network_chunks() {
        let payload =
            "data: {\"type\":\"run.completed\",\"summary\":\"已生成文档\"}\n\n".as_bytes();
        let chinese_start = payload
            .windows("已".len())
            .position(|window| window == "已".as_bytes())
            .unwrap();
        let split_at = chinese_start + 1;
        let mut buffer = Vec::new();

        assert!(append_sse_chunk(&mut buffer, &payload[..split_at]).is_empty());
        let events = append_sse_chunk(&mut buffer, &payload[split_at..]);

        assert_eq!(
            events,
            vec!["{\"type\":\"run.completed\",\"summary\":\"已生成文档\"}"]
        );
        assert!(buffer.is_empty());
    }

    #[test]
    fn creation_tool_contract_keeps_required_tools_enabled() {
        let request: GenerateRequest = serde_json::from_value(serde_json::json!({
            "user_prompt": "生成架构方案",
            "enabled_tools": ["plantuml_diagram", "memory_search", ""]
        }))
        .unwrap();

        assert_eq!(request.max_references, 10);
        assert_eq!(request.data_search_limit, 30);

        assert_eq!(
            normalize_creation_tool_ids(request.enabled_tools),
            vec![
                "internet_search".to_string(),
                "memory_search".to_string(),
                "data_search".to_string(),
                "webpage_scrape".to_string(),
                "plantuml_diagram".to_string(),
            ]
        );
    }
}
