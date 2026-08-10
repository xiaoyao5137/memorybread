use std::{
    collections::HashSet,
    sync::{atomic::Ordering, Arc},
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{
    extract::{Path, Query, Request, State},
    http::StatusCode,
    Json,
};
use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use tracing::warn;

use crate::{
    api::{
        error::ApiError,
        state::{AppState, CreationSkillAnalysisJobRecord},
    },
    storage::repo::creation_skill::{
        CreationSkillDescription, CreationSkillDistinctiveSection, CreationSkillExecutionStep,
        CreationSkillFieldExamples, CreationSkillRecord, CreationSkillSectionHeadings,
        UpsertCreationSkill,
    },
};

#[derive(Debug, Clone, Deserialize)]
pub struct AnalyzeCreationSkillRequest {
    pub source_kind: String,
    pub source_id: String,
    pub document_title: String,
    pub document_content: String,
    #[serde(default)]
    pub doc_type: String,
}

#[derive(Debug, Serialize)]
struct AnalyzeCreationSkillPayload<'a> {
    document_title: &'a str,
    document_content: &'a str,
    doc_type: &'a str,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreationSkillAnalysis {
    pub title: String,
    pub summary: String,
    pub common_titles: Vec<String>,
    pub title_style: String,
    pub text_style: String,
    pub diagram_style: String,
    #[serde(default)]
    pub writing_guidelines: Vec<String>,
    #[serde(default)]
    pub distinctive_sections: Vec<CreationSkillDistinctiveSection>,
    #[serde(default)]
    pub section_headings: CreationSkillSectionHeadings,
    #[serde(default)]
    pub field_examples: CreationSkillFieldExamples,
    #[serde(default = "default_analysis_example_document")]
    pub example_document: String,
    #[serde(default)]
    pub skill_description: CreationSkillDescription,
    #[serde(default)]
    pub execution_steps: Vec<CreationSkillExecutionStep>,
    #[serde(default)]
    pub suggested_category_keywords: Vec<String>,
    #[serde(default)]
    pub analysis_mode: String,
    #[serde(default)]
    pub fallback_reason: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CreationSkillAnalysisJobCreateResponse {
    pub job_id: String,
    pub status: String,
}

#[derive(Debug, Serialize)]
pub struct CreationSkillAnalysisJobStatusResponse {
    pub id: String,
    pub status: String,
    pub result: Option<serde_json::Value>,
    pub error_code: Option<String>,
    pub error: Option<String>,
    pub created_at_ms: i64,
    pub updated_at_ms: i64,
}

fn default_analysis_example_document() -> String {
    r#"# 共享评审空间：预约流程与协作边界优化方案

## 摘要

本文围绕一个完全虚构的共享评审空间场景，讨论预约信息分散、资源状态不透明和异常处理依赖口头协调的问题。方案的重点不是增加审批，而是让每次申请都能回答三个问题：当前由谁使用、下一步由谁处理、完成后凭什么确认资源已经释放。

全文先界定问题和适用范围，再把目标拆成可观察状态，随后给出角色分工、核心流程、异常保障与验证方式。所有判断都落到动作和证据，不使用真实组织、项目或业务数据。

## 背景与问题：一次冲突暴露出的状态断点

共享评审空间同时服务准备材料、集中讨论和结果确认等活动。现有做法只记录“有人预约”，却没有说明准备是否完成、临时变更是否被接收、使用结束后资源是否已经恢复。信息看似存在，真正执行时仍要逐人询问。

问题的核心不是缺少一张登记表，而是状态、动作和责任没有对应关系。申请角色关心能否使用，维护角色关心是否满足开放条件，后续使用者关心资源何时重新可用；如果这些问题混在一个备注框里，任何变更都会重新触发人工确认。

## 目标与范围：先明确要解决什么

本次优化只处理预约发起、冲突确认、使用准备、完成释放和异常复核。目标是让相关角色不依赖额外询问，也能从同一处判断当前状态、待办动作和完成证据。界面样式、空间硬件和人员排班不在本次方案范围内。

需要明确的是，范围约束不是附注，而是后续取舍的依据。凡是不能改变状态判断、责任归属或验证结果的信息，都不进入主流程；确需保留的补充说明放在对应动作之后，避免重要条件被长段背景淹没。

## 方案设计：让状态、责任与动作相互对应

方案把一次预约拆成“申请、确认、准备、使用、释放、复核”几个连续状态。每个状态都绑定进入条件、责任角色、应执行动作和完成证据；只有证据满足要求，状态才向后流转。这样既能保持流程简洁，也能避免角色凭经验猜测。

角色分工遵循“谁产生信息，谁负责首次更新；谁消费结果，谁负责确认可用”的原则：

- 申请角色说明使用目的、期望范围和必要准备，并对变更及时更新。
- 维护角色检查冲突与开放条件，只对自己能够验证的状态作确认。
- 使用角色在开始前确认资源状态，在结束后提交释放结果和遗留事项。
- 复核角色只处理异常和争议，不重复参与每一次正常流转。

## 核心流程：从提出申请到完成释放

流程从申请角色提交用途和范围开始。系统先检查同一时段是否存在冲突；没有冲突时进入准备状态，有冲突时返回可调整的条件，而不是只给出“失败”结果。申请角色据此修改范围或撤回请求，避免维护角色在多个沟通渠道间转述。

随后，准备完成后由使用角色确认接手。确认动作意味着必要材料、访问边界和现场状态已经可用，而不是简单点击按钮。使用结束后，使用角色提交释放结果；若仍有遗留事项，则同时标明影响范围和下一位处理角色，流程不会把“已结束”误写成“已恢复”。

## 风险与保障：异常不能重新回到人工猜测

主要风险来自三类断点：状态被更新但相关角色没有接收、异常被记录却没有明确下一步、完成结果缺少可复核证据。对应保障也不应写成宽泛口号，而要直接嵌入流程。

- 关键状态变化只保留一个正式入口，其他渠道只发送提醒，不形成第二份事实。
- 异常记录必须同时包含影响范围、临时处理和下一位责任角色。
- 释放动作必须附带可观察结果；无法确认时回到复核状态，不直接标记完成。
- 长时间没有推进的事项进入待复核列表，由相关角色判断继续、调整或关闭。

## 验证与复盘：用可观察结果收束判断

验证分为流程可执行性和结果可判断性。前者关注相关角色能否只凭当前记录完成下一步，后者关注状态变化是否都有对应证据。试运行期间不追求覆盖所有例外，而是优先验证主流程是否连续、异常是否能回到明确责任人。

复盘时按“现象、判断、动作、结果”记录，不把意见数量当作效果。若某个节点仍需要反复口头确认，应先检查进入条件是否含糊；若不同角色对完成状态理解不一，应先修正证据定义，而不是继续增加提醒。

## 结论与后续：把临时协调变成稳定机制

这套方案把一次临时协调转化为可以被读取、执行和复核的状态链路。它保留必要的人为判断，但让判断发生在边界明确的位置；它减少重复询问，但不以隐藏异常为代价。

后续优化应继续围绕同一目标展开：让每位相关角色在进入流程时知道自己为什么接手、需要完成什么、完成后留下什么证据。只要这三个问题能够稳定回答，共享资源的协作就不再依赖某位熟悉情况的人持续兜底。"#
        .to_string()
}

#[derive(Debug, Default, Deserialize)]
pub struct CreationSkillQuery {
    #[serde(default)]
    pub source_kind: Option<String>,
    #[serde(default)]
    pub source_id: Option<String>,
    #[serde(default)]
    pub installed: Option<bool>,
}

pub async fn analyze_creation_skill(
    State(state): State<Arc<AppState>>,
    Json(request): Json<AnalyzeCreationSkillRequest>,
) -> Result<Json<CreationSkillAnalysis>, ApiError> {
    validate_analysis_request(&request)?;
    Ok(Json(
        call_creation_skill_analyzer(&state.creation_sidecar_url, &request).await?,
    ))
}

pub async fn create_creation_skill_analysis_job(
    State(state): State<Arc<AppState>>,
    Json(request): Json<AnalyzeCreationSkillRequest>,
) -> Result<Json<CreationSkillAnalysisJobCreateResponse>, ApiError> {
    validate_analysis_request(&request)?;

    let seq = state
        .creation_skill_analysis_job_seq
        .fetch_add(1, Ordering::Relaxed);
    let created_at_ms = now_ms();
    let job_id = format!("creation-skill-analysis-{created_at_ms}-{seq}");
    let record = CreationSkillAnalysisJobRecord {
        id: job_id.clone(),
        status: "pending".to_string(),
        result: None,
        error_code: None,
        error: None,
        created_at_ms,
        updated_at_ms: created_at_ms,
    };

    {
        let mut jobs = state
            .creation_skill_analysis_jobs
            .lock()
            .map_err(|_| ApiError::Internal("技能分析任务状态锁异常".to_string()))?;
        jobs.insert(job_id.clone(), record);
    }

    let state_for_task = state.clone();
    let job_id_for_task = job_id.clone();
    let creation_sidecar_url = state.creation_sidecar_url.clone();
    tokio::spawn(async move {
        set_creation_skill_analysis_job(
            &state_for_task,
            &job_id_for_task,
            "running",
            None,
            None,
            None,
        );
        match call_creation_skill_analyzer(&creation_sidecar_url, &request).await {
            Ok(analysis) => set_creation_skill_analysis_job(
                &state_for_task,
                &job_id_for_task,
                "succeeded",
                serde_json::to_value(analysis).ok(),
                None,
                None,
            ),
            Err(error) => {
                let (code, message) = creation_skill_analysis_error(&error);
                set_creation_skill_analysis_job(
                    &state_for_task,
                    &job_id_for_task,
                    "failed",
                    None,
                    Some(code),
                    Some(message),
                );
            }
        }
    });

    Ok(Json(CreationSkillAnalysisJobCreateResponse {
        job_id,
        status: "pending".to_string(),
    }))
}

pub async fn get_creation_skill_analysis_job(
    State(state): State<Arc<AppState>>,
    Path(job_id): Path<String>,
) -> Result<Json<CreationSkillAnalysisJobStatusResponse>, ApiError> {
    let jobs = state
        .creation_skill_analysis_jobs
        .lock()
        .map_err(|_| ApiError::Internal("技能分析任务状态锁异常".to_string()))?;
    let job = jobs
        .get(&job_id)
        .ok_or_else(|| ApiError::NotFound("技能分析任务不存在或已过期".to_string()))?;

    Ok(Json(CreationSkillAnalysisJobStatusResponse {
        id: job.id.clone(),
        status: job.status.clone(),
        result: job.result.clone(),
        error_code: job.error_code.clone(),
        error: job.error.clone(),
        created_at_ms: job.created_at_ms,
        updated_at_ms: job.updated_at_ms,
    }))
}

fn validate_analysis_request(request: &AnalyzeCreationSkillRequest) -> Result<(), ApiError> {
    validate_source(&request.source_kind, &request.source_id)?;
    let title = request.document_title.trim();
    let content = request.document_content.trim();
    if title.is_empty() || title.chars().count() > 200 {
        return Err(ApiError::BadRequest(
            "文档标题需要在 1 到 200 个字符之间".into(),
        ));
    }
    if content.chars().count() < 20 || content.chars().count() > 80_000 {
        return Err(ApiError::BadRequest(
            "文档内容需要在 20 到 80000 个字符之间".into(),
        ));
    }
    Ok(())
}

async fn call_creation_skill_analyzer(
    creation_sidecar_url: &str,
    request: &AnalyzeCreationSkillRequest,
) -> Result<CreationSkillAnalysis, ApiError> {
    let title = request.document_title.trim();
    let content = request.document_content.trim();
    let response = reqwest::Client::new()
        .post(format!(
            "{}/creation/skills/analyze",
            creation_sidecar_url.trim_end_matches('/')
        ))
        .json(&AnalyzeCreationSkillPayload {
            document_title: title,
            document_content: content,
            doc_type: request.doc_type.trim(),
        })
        .send()
        .await
        .map_err(|error| ApiError::Upstream {
            status: StatusCode::BAD_GATEWAY,
            code: "CREATION_SKILL_ANALYZER_UNAVAILABLE",
            message: format!("本地技能分析服务不可用: {error}"),
        })?;
    if !response.status().is_success() {
        let message = response.text().await.unwrap_or_default();
        return Err(ApiError::Upstream {
            status: StatusCode::BAD_GATEWAY,
            code: "CREATION_SKILL_ANALYSIS_FAILED",
            message: if message.is_empty() {
                "本地技能分析失败".to_string()
            } else {
                message
            },
        });
    }
    let analysis = response
        .json::<CreationSkillAnalysis>()
        .await
        .map_err(|error| ApiError::Upstream {
            status: StatusCode::BAD_GATEWAY,
            code: "INVALID_CREATION_SKILL_ANALYSIS",
            message: format!("本地技能分析结果格式错误: {error}"),
        })?;
    validate_analysis(&analysis)?;
    Ok(analysis)
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i64)
        .unwrap_or_default()
}

fn creation_skill_analysis_error(error: &ApiError) -> (String, String) {
    match error {
        ApiError::Upstream { code, message, .. } => (code.to_string(), message.clone()),
        ApiError::BadRequest(message) => ("BAD_REQUEST".to_string(), message.clone()),
        ApiError::NotFound(message) => ("NOT_FOUND".to_string(), message.clone()),
        ApiError::Internal(message) => ("INTERNAL_ERROR".to_string(), message.clone()),
        ApiError::Storage(_) => ("STORAGE_ERROR".to_string(), "数据库操作失败".to_string()),
    }
}

fn set_creation_skill_analysis_job(
    state: &Arc<AppState>,
    job_id: &str,
    status: &str,
    result: Option<serde_json::Value>,
    error_code: Option<String>,
    error: Option<String>,
) {
    if let Ok(mut jobs) = state.creation_skill_analysis_jobs.lock() {
        if let Some(job) = jobs.get_mut(job_id) {
            job.status = status.to_string();
            job.result = result;
            job.error_code = error_code;
            job.error = error;
            job.updated_at_ms = now_ms();
        }
    }
}

pub async fn list_creation_skills(
    State(state): State<Arc<AppState>>,
    Query(query): Query<CreationSkillQuery>,
) -> Result<Json<Vec<CreationSkillRecord>>, ApiError> {
    if query.source_kind.is_some() != query.source_id.is_some() {
        return Err(ApiError::BadRequest(
            "按来源查询技能时需要同时提供来源类型和来源标识".into(),
        ));
    }
    if let (Some(source_kind), Some(source_id)) = (&query.source_kind, &query.source_id) {
        validate_persisted_source(source_kind, source_id)?;
    }
    Ok(Json(state.storage.list_creation_skills_filtered(
        query.source_kind.as_deref(),
        query.source_id.as_deref(),
        query.installed,
    )?))
}

pub async fn get_creation_skill(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<Json<CreationSkillRecord>, ApiError> {
    state
        .storage
        .get_creation_skill(id)?
        .map(Json)
        .ok_or_else(|| ApiError::NotFound("技能不存在".into()))
}

const CREATION_SKILL_BODY_LIMIT_BYTES: usize = 16 * 1024 * 1024;

/// 手工读取并解析请求体，保证任何解析失败都返回带具体原因的 JSON 错误，
/// 而不是 axum 默认的纯文本拒绝响应（前端无法展示原因）。
async fn parse_skill_json(request: Request) -> Result<UpsertCreationSkill, ApiError> {
    let content_type = request
        .headers()
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .to_string();
    if !content_type.starts_with("application/json") {
        return Err(ApiError::BadRequest(
            "保存技能需要发送 application/json 请求体".into(),
        ));
    }
    let bytes = axum::body::to_bytes(request.into_body(), CREATION_SKILL_BODY_LIMIT_BYTES)
        .await
        .map_err(|error| {
            ApiError::BadRequest(format!("技能请求体过大或读取失败（上限 16 MB）: {error}"))
        })?;
    serde_json::from_slice(&bytes).map_err(|error| {
        ApiError::BadRequest(format!("技能内容格式不正确，无法解析请求体: {error}"))
    })
}

fn skill_storage_error(endpoint: &str, error: &crate::storage::StorageError) -> ApiError {
    tracing::error!("{endpoint} 写入技能失败: {error}");
    let message = error.to_string();
    let hint = if message.contains("CHECK constraint failed: source_kind") {
        "：当前版本数据库表还不支持该来源类型，请更新应用后重试"
    } else if message.contains("CHECK constraint failed") {
        "：字段取值不符合存储约束，请检查技能状态与来源类型"
    } else {
        "：本地数据库写入失败"
    };
    ApiError::Internal(format!("保存技能失败{hint}（{message}）"))
}

pub async fn save_creation_skill(
    State(state): State<Arc<AppState>>,
    request: Request,
) -> Result<(StatusCode, Json<CreationSkillRecord>), ApiError> {
    let skill = parse_skill_json(request).await?;
    validate_persisted_source(&skill.source_kind, &skill.source_id)
        .map_err(|error| log_skill_validation("POST /api/creation/skills", &skill, error))?;
    validate_skill_input(&skill)
        .map_err(|error| log_skill_validation("POST /api/creation/skills", &skill, error))?;
    let saved = state
        .storage
        .upsert_creation_skill(&skill)
        .map_err(|error| skill_storage_error("POST /api/creation/skills", &error))?;
    Ok((StatusCode::CREATED, Json(saved)))
}

pub async fn update_creation_skill(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
    request: Request,
) -> Result<Json<CreationSkillRecord>, ApiError> {
    let existing = state
        .storage
        .get_creation_skill(id)?
        .ok_or_else(|| ApiError::NotFound("技能不存在".into()))?;
    let mut skill = parse_skill_json(request).await?;
    validate_persisted_source(&skill.source_kind, &skill.source_id)
        .map_err(|error| log_skill_validation("PUT /api/creation/skills/:id", &skill, error))?;
    skill.client_skill_key = existing.client_skill_key;
    validate_skill_input(&skill)
        .map_err(|error| log_skill_validation("PUT /api/creation/skills/:id", &skill, error))?;
    state
        .storage
        .upsert_creation_skill(&skill)
        .map(Json)
        .map_err(|error| skill_storage_error("PUT /api/creation/skills/:id", &error))
}

fn log_skill_validation(endpoint: &str, skill: &UpsertCreationSkill, error: ApiError) -> ApiError {
    warn!(
        "技能保存校验失败: endpoint={endpoint} key={} source_kind={} status={} reason={error}",
        skill.client_skill_key, skill.source_kind, skill.status
    );
    error
}

pub async fn delete_creation_skill(
    State(state): State<Arc<AppState>>,
    Path(id): Path<i64>,
) -> Result<StatusCode, ApiError> {
    let existing = state
        .storage
        .get_creation_skill(id)?
        .ok_or_else(|| ApiError::NotFound("技能不存在".into()))?;
    if existing.published {
        return Err(ApiError::BadRequest(
            "请先从技能市场下架，再删除本地技能".into(),
        ));
    }
    if state.storage.delete_creation_skill(id)? {
        Ok(StatusCode::NO_CONTENT)
    } else {
        Err(ApiError::NotFound("技能不存在".into()))
    }
}

fn validate_skill_input(skill: &UpsertCreationSkill) -> Result<(), ApiError> {
    let bad = |reason: &str| Err(ApiError::BadRequest(reason.to_string()));

    fn list_issue(items: &[String], min: usize, max: usize, item_max: usize) -> Option<String> {
        if items.len() < min {
            return Some(format!("至少需要 {min} 条"));
        }
        if items.len() > max {
            return Some(format!(
                "最多 {max} 条，当前 {count} 条",
                count = items.len()
            ));
        }
        for item in items {
            let trimmed = item.trim();
            if trimmed.is_empty() {
                return Some("存在空条目".to_string());
            }
            if trimmed.chars().count() > item_max {
                return Some(format!("单条超过 {item_max} 个字符"));
            }
        }
        None
    }
    let require_list = |items: &[String], min: usize, max: usize, item_max: usize, name: &str| {
        list_issue(items, min, max, item_max).map(|issue| format!("{name}{issue}"))
    };
    let require_text = |value: &str, max: usize, name: &str| {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            return Some(format!("{name}不能为空"));
        }
        if trimmed.chars().count() > max {
            return Some(format!(
                "{name}超过 {max} 个字符，当前 {count} 个字符",
                count = trimmed.chars().count()
            ));
        }
        None
    };
    // 创作配方与示例文档在界面上允许留空；有内容时仍受长度上限约束。
    let optional_text = |value: &str, max: usize, name: &str| {
        let trimmed = value.trim();
        if trimmed.is_empty() {
            return None;
        }
        if trimmed.chars().count() > max {
            return Some(format!(
                "{name}超过 {max} 个字符，当前 {count} 个字符",
                count = trimmed.chars().count()
            ));
        }
        None
    };

    if skill.client_skill_key.trim().is_empty() {
        return bad("技能标识不能为空");
    }
    if skill.client_skill_key.chars().count() > 80 {
        return bad("技能标识超过 80 个字符");
    }
    if let Some(reason) = require_text(&skill.title, 80, "技能标题") {
        return bad(&reason);
    }
    if let Some(reason) = require_text(&skill.summary, 400, "技能简介") {
        return bad(&reason);
    }
    if let Some(reason) = require_list(&skill.common_titles, 0, 12, 80, "章节标题示例") {
        return bad(&reason);
    }
    if let Some(reason) = optional_text(&skill.title_style, 1_200, "标题句式示例") {
        return bad(&reason);
    }
    if let Some(reason) = optional_text(&skill.text_style, 2_000, "行文思路示例") {
        return bad(&reason);
    }
    if let Some(reason) = optional_text(&skill.diagram_style, 1_200, "图示风格示例") {
        return bad(&reason);
    }
    if let Some(reason) = require_list(&skill.writing_guidelines, 0, 16, 240, "惯用话术示例")
    {
        return bad(&reason);
    }
    if let Some(reason) = distinctive_sections_issue(&skill.distinctive_sections) {
        return bad(&reason);
    }
    if let Some(reason) = skill_description_issue(&skill.skill_description) {
        return bad(&reason);
    }
    if let Some(reason) = execution_steps_issue(&skill.execution_steps) {
        return bad(&reason);
    }
    if !matches!(
        skill.section_headings.common_titles.as_str(),
        "标题设计风格" | "这类文档标题通常怎么命名"
    ) {
        return bad("章节标题设计类型取值不正确");
    }
    let headings = &skill.section_headings;
    if let Some(reason) = require_text(&headings.title_style, 120, "章节标题设计·标题句式")
    {
        return bad(&reason);
    }
    if let Some(reason) = require_text(&headings.text_style, 120, "章节标题设计·行文思路")
    {
        return bad(&reason);
    }
    if let Some(reason) = require_text(&headings.diagram_style, 120, "章节标题设计·图示风格")
    {
        return bad(&reason);
    }
    if let Some(reason) = require_text(&headings.writing_guidelines, 120, "章节标题设计·惯用话术")
    {
        return bad(&reason);
    }
    let examples = &skill.field_examples;
    if let Some(reason) = require_list(&examples.common_titles, 0, 6, 240, "字段示例·章节标题")
    {
        return bad(&reason);
    }
    if let Some(reason) = require_list(&examples.title_style, 0, 6, 500, "字段示例·标题句式")
    {
        return bad(&reason);
    }
    if let Some(reason) = require_list(&examples.text_style, 0, 6, 500, "字段示例·行文思路")
    {
        return bad(&reason);
    }
    if let Some(reason) = require_list(&examples.diagram_style, 0, 6, 500, "字段示例·图示风格")
    {
        return bad(&reason);
    }
    if let Some(reason) = require_list(&examples.writing_guidelines, 0, 6, 500, "字段示例·惯用话术")
    {
        return bad(&reason);
    }
    // 完整示例文档允许留空；有内容时保持 100 到 12000 个字符区间。
    let example_len = skill.example_document.trim().chars().count();
    if example_len > 0 && example_len < 100 {
        return bad("完整示例文档需要在 100 到 12000 个字符之间，当前内容过短");
    }
    if example_len > 12_000 {
        return bad("完整示例文档需要在 100 到 12000 个字符之间，当前内容过长");
    }
    if !matches!(skill.status.as_str(), "draft" | "saved") {
        return bad("技能状态只能是 draft 或 saved");
    }
    if skill.installed && skill.status != "saved" {
        return bad("技能需要先保存（status=saved）才能安装");
    }
    if skill.published && skill.status != "saved" {
        return bad("技能需要先保存（status=saved）才能发布");
    }

    validate_package_files(skill)?;
    if skill.published
        && (skill
            .cloud_skill_id
            .as_deref()
            .unwrap_or_default()
            .is_empty()
            || skill.category_id.as_deref().unwrap_or_default().is_empty())
    {
        return bad("已公开技能需要关联云端标识和第四级类目");
    }
    Ok(())
}

fn distinctive_sections_issue(sections: &[CreationSkillDistinctiveSection]) -> Option<String> {
    if sections.len() > 6 {
        return Some(format!(
            "特色章节最多 6 个，当前 {count} 个",
            count = sections.len()
        ));
    }
    for (index, section) in sections.iter().enumerate() {
        let position = index + 1;
        if section.title.trim().is_empty() || section.title.trim().chars().count() > 80 {
            return Some(format!("特色章节 {position} 的标题需要为 1 到 80 个字符"));
        }
        if section.description.trim().is_empty()
            || section.description.trim().chars().count() > 1_200
        {
            return Some(format!("特色章节 {position} 的说明需要为 1 到 1200 个字符"));
        }
        if section.guidance.trim().is_empty() || section.guidance.trim().chars().count() > 1_200 {
            return Some(format!(
                "特色章节 {position} 的写法指引需要为 1 到 1200 个字符"
            ));
        }
        if section.examples.is_empty() || section.examples.len() > 6 {
            return Some(format!("特色章节 {position} 需要 1 到 6 条仿写示例"));
        }
        if section
            .examples
            .iter()
            .any(|example| example.trim().is_empty() || example.trim().chars().count() > 800)
        {
            return Some(format!(
                "特色章节 {position} 的仿写示例需要为 1 到 800 个字符"
            ));
        }
    }
    None
}

fn skill_description_issue(description: &CreationSkillDescription) -> Option<String> {
    let legacy_empty = description.purpose.trim().is_empty()
        && description.document_types.is_empty()
        && description.problems.is_empty()
        && description.domains.is_empty()
        && description.deliverables.is_empty();
    if legacy_empty {
        return None;
    }
    if description.purpose.trim().is_empty() || description.purpose.trim().chars().count() > 1_200 {
        return Some("技能用途说明需要为 1 到 1200 个字符".to_string());
    }
    if let Some(issue) = simple_list_issue(&description.document_types, 1, 12, 120) {
        return Some(format!("适用文档类型{issue}"));
    }
    if let Some(issue) = simple_list_issue(&description.problems, 1, 12, 240) {
        return Some(format!("解决的问题{issue}"));
    }
    if let Some(issue) = simple_list_issue(&description.domains, 0, 12, 120) {
        return Some(format!("适用领域{issue}"));
    }
    if let Some(issue) = simple_list_issue(&description.deliverables, 1, 12, 240) {
        return Some(format!("交付物{issue}"));
    }
    None
}

fn simple_list_issue(items: &[String], min: usize, max: usize, item_max: usize) -> Option<String> {
    if items.len() < min {
        return Some(format!("至少需要 {min} 条"));
    }
    if items.len() > max {
        return Some(format!("最多 {max} 条"));
    }
    if items
        .iter()
        .any(|item| item.trim().is_empty() || item.trim().chars().count() > item_max)
    {
        return Some(format!("单条需要为 1 到 {item_max} 个字符"));
    }
    None
}

fn execution_steps_issue(steps: &[CreationSkillExecutionStep]) -> Option<String> {
    if steps.is_empty() {
        return None;
    }
    if steps.len() > 12 {
        return Some(format!(
            "执行步骤最多 12 步，当前 {count} 步",
            count = steps.len()
        ));
    }
    for (index, step) in steps.iter().enumerate() {
        let position = index + 1;
        if !valid_identifier(&step.id) {
            return Some(format!(
                "执行步骤 {position} 的标识只能包含小写字母、数字、下划线和连字符，且不超过 80 个字符"
            ));
        }
        if step.title.trim().is_empty() || step.title.trim().chars().count() > 80 {
            return Some(format!("执行步骤 {position} 的标题需要为 1 到 80 个字符"));
        }
        if step.objective.trim().is_empty() || step.objective.trim().chars().count() > 500 {
            return Some(format!(
                "执行步骤 {position} 的执行动作需要为 1 到 500 个字符"
            ));
        }
        // 步骤目标与产出在界面上合并为“执行动作”单字段，产出兼容为空。
        if step.output.trim().chars().count() > 240 {
            return Some(format!("执行步骤 {position} 的步骤产出不能超过 240 个字符"));
        }
        if let Some(issue) = simple_list_issue(&step.agents, 0, 8, 80) {
            return Some(format!("执行步骤 {position} 引用的 Agent {issue}"));
        }
        if let Some(issue) = simple_list_issue(&step.skills, 0, 8, 80) {
            return Some(format!("执行步骤 {position} 引用的 Skill {issue}"));
        }
        if let Some(issue) = simple_list_issue(&step.tools, 0, 8, 80) {
            return Some(format!("执行步骤 {position} 引用的 Tool {issue}"));
        }
        if step.agents.len() + step.tools.len() > 4 {
            return Some(format!(
                "执行步骤 {position} 引用的 Agent 与 Tool 总数不能超过 4 个"
            ));
        }
    }
    None
}

fn valid_skill_description(description: &CreationSkillDescription) -> bool {
    let legacy_empty = description.purpose.trim().is_empty()
        && description.document_types.is_empty()
        && description.problems.is_empty()
        && description.domains.is_empty()
        && description.deliverables.is_empty();
    legacy_empty
        || (valid_text(&description.purpose, 1, 1_200)
            && valid_string_list(&description.document_types, 1, 12, 120)
            && valid_string_list(&description.problems, 1, 12, 240)
            && valid_string_list(&description.domains, 0, 12, 120)
            && valid_string_list(&description.deliverables, 1, 12, 240))
}

fn valid_execution_steps(steps: &[CreationSkillExecutionStep]) -> bool {
    steps.is_empty()
        || (steps.len() <= 12
            && steps.iter().all(|step| {
                valid_identifier(&step.id)
                    && valid_text(&step.title, 1, 80)
                    && valid_text(&step.objective, 1, 500)
                    && valid_text(&step.output, 0, 240)
                    && valid_string_list(&step.agents, 0, 8, 80)
                    && valid_string_list(&step.skills, 0, 8, 80)
                    && valid_string_list(&step.tools, 0, 8, 80)
                    && step.agents.len() + step.tools.len() <= 4
            }))
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.chars().count() <= 80
        && value.chars().all(|character| {
            character.is_ascii_lowercase()
                || character.is_ascii_digit()
                || character == '_'
                || character == '-'
        })
}

fn valid_text(value: &str, min: usize, max: usize) -> bool {
    let count = value.trim().chars().count();
    count >= min && count <= max
}

fn valid_string_list(items: &[String], min: usize, max: usize, item_max: usize) -> bool {
    items.len() >= min
        && items.len() <= max
        && items.iter().all(|item| valid_text(item, 1, item_max))
}

fn validate_package_files(skill: &UpsertCreationSkill) -> Result<(), ApiError> {
    const MAX_FILE_COUNT: usize = 128;
    const MAX_FILE_BYTES: usize = 5 * 1024 * 1024;
    const MAX_TOTAL_BYTES: usize = 10 * 1024 * 1024;
    const MAX_SKILL_MD_BYTES: usize = 512 * 1024;

    if skill.package_files.is_empty() {
        if skill.source_kind == "imported" {
            return Err(ApiError::BadRequest(
                "上传的技能包必须包含根目录 SKILL.md".into(),
            ));
        }
        return Ok(());
    }
    if skill.package_files.len() > MAX_FILE_COUNT {
        return Err(ApiError::BadRequest("技能包文件数量不能超过 128 个".into()));
    }

    let mut seen = HashSet::new();
    let mut total_bytes = 0usize;
    let mut skill_markdown = None;
    for file in &skill.package_files {
        let path = file.path.trim();
        let valid_path = !path.is_empty()
            && path.chars().count() <= 240
            && !path.starts_with('/')
            && !path.contains('\\')
            && !path.chars().any(char::is_control)
            && path
                .split('/')
                .all(|part| !part.is_empty() && part != "." && part != "..");
        if !valid_path || !seen.insert(path.to_string()) {
            return Err(ApiError::BadRequest(
                "技能包包含无效或重复的文件路径".into(),
            ));
        }
        if file.media_type.trim().is_empty() || file.media_type.chars().count() > 160 {
            return Err(ApiError::BadRequest("技能包文件类型不正确".into()));
        }
        let bytes = BASE64_STANDARD
            .decode(&file.content_base64)
            .map_err(|_| ApiError::BadRequest("技能包文件内容格式不正确".into()))?;
        if bytes.len() != file.size_bytes as usize || bytes.len() > MAX_FILE_BYTES {
            return Err(ApiError::BadRequest(
                "技能包文件大小不正确或超过 5 MB".into(),
            ));
        }
        total_bytes = total_bytes.saturating_add(bytes.len());
        if total_bytes > MAX_TOTAL_BYTES {
            return Err(ApiError::BadRequest("技能包总大小不能超过 10 MB".into()));
        }
        if path == "SKILL.md" {
            if bytes.len() > MAX_SKILL_MD_BYTES {
                return Err(ApiError::BadRequest("SKILL.md 不能超过 512 KB".into()));
            }
            skill_markdown = Some(
                String::from_utf8(bytes)
                    .map_err(|_| ApiError::BadRequest("SKILL.md 必须使用 UTF-8 编码".into()))?,
            );
        }
    }

    let skill_markdown =
        skill_markdown.ok_or_else(|| ApiError::BadRequest("技能包根目录缺少 SKILL.md".into()))?;
    let package_name = validate_skill_markdown(&skill_markdown)?;
    if skill.source_kind == "imported" && skill.source_id != package_name {
        return Err(ApiError::BadRequest(
            "SKILL.md 的 name 必须与技能目录名称一致".into(),
        ));
    }
    Ok(())
}

fn validate_skill_markdown(content: &str) -> Result<String, ApiError> {
    let name = frontmatter_value(content, "name")
        .ok_or_else(|| ApiError::BadRequest("SKILL.md 缺少 name 元数据".into()))?;
    let description = frontmatter_value(content, "description")
        .ok_or_else(|| ApiError::BadRequest("SKILL.md 缺少 description 元数据".into()))?;
    let valid_name = !name.is_empty()
        && name.len() <= 64
        && !name.starts_with('-')
        && !name.ends_with('-')
        && !name.contains("--")
        && name
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-');
    if !valid_name {
        return Err(ApiError::BadRequest(
            "SKILL.md 的 name 必须是 1 到 64 位小写字母、数字或连字符".into(),
        ));
    }
    if description.is_empty() || description.chars().count() > 1_024 {
        return Err(ApiError::BadRequest(
            "SKILL.md 的 description 需要在 1 到 1024 个字符之间".into(),
        ));
    }
    Ok(name)
}

fn frontmatter_value(content: &str, key: &str) -> Option<String> {
    let normalized = content.trim_start_matches('\u{feff}').replace("\r\n", "\n");
    let lines = normalized.lines().collect::<Vec<_>>();
    if lines.first()?.trim() != "---" {
        return None;
    }
    let closing_index = lines[1..]
        .iter()
        .position(|line| line.trim() == "---")
        .map(|index| index + 1)?;
    let frontmatter = &lines[1..closing_index];
    let prefix = format!("{key}:");
    for (index, line) in frontmatter.iter().enumerate() {
        let trimmed = line.trim_start();
        let Some(raw) = trimmed.strip_prefix(&prefix) else {
            continue;
        };
        let value = raw.trim();
        if matches!(value, "|" | "|-" | "|+" | ">" | ">-" | ">+") {
            let continuation = frontmatter[index + 1..]
                .iter()
                .take_while(|next| next.starts_with(' ') || next.starts_with('\t'))
                .map(|next| next.trim())
                .collect::<Vec<_>>();
            return Some(if value.starts_with('>') {
                continuation.join(" ")
            } else {
                continuation.join("\n")
            });
        }
        let unquoted = if (value.starts_with('"') && value.ends_with('"'))
            || (value.starts_with('\'') && value.ends_with('\''))
        {
            value[1..value.len().saturating_sub(1)].trim()
        } else {
            value
                .split_once(" #")
                .map_or(value, |(plain, _)| plain)
                .trim()
        };
        return Some(unquoted.to_string());
    }
    None
}

fn validate_source(source_kind: &str, source_id: &str) -> Result<(), ApiError> {
    if !matches!(source_kind, "creation_history" | "bake_document") {
        return Err(ApiError::BadRequest("技能来源类型不受支持".into()));
    }
    if source_id.trim().is_empty() || source_id.chars().count() > 80 {
        return Err(ApiError::BadRequest("技能来源标识不正确".into()));
    }
    Ok(())
}

fn validate_persisted_source(source_kind: &str, source_id: &str) -> Result<(), ApiError> {
    if !matches!(
        source_kind,
        "creation_history" | "bake_document" | "market" | "imported" | "manual"
    ) {
        return Err(ApiError::BadRequest("技能来源类型不受支持".into()));
    }
    if source_id.trim().is_empty() || source_id.chars().count() > 80 {
        return Err(ApiError::BadRequest("技能来源标识不正确".into()));
    }
    Ok(())
}

fn validate_analysis(analysis: &CreationSkillAnalysis) -> Result<(), ApiError> {
    if analysis.title.trim().is_empty()
        || analysis.summary.trim().is_empty()
        || analysis.common_titles.is_empty()
        || analysis.title_style.trim().is_empty()
        || analysis.text_style.trim().is_empty()
        || analysis.diagram_style.trim().is_empty()
        || !valid_distinctive_sections(&analysis.distinctive_sections)
        || !valid_skill_description(&analysis.skill_description)
        || !valid_execution_steps(&analysis.execution_steps)
        || !matches!(
            analysis.section_headings.common_titles.as_str(),
            "标题设计风格" | "这类文档标题通常怎么命名"
        )
        || analysis.section_headings.title_style.trim().is_empty()
        || analysis.section_headings.text_style.trim().is_empty()
        || analysis.section_headings.diagram_style.trim().is_empty()
        || analysis
            .section_headings
            .writing_guidelines
            .trim()
            .is_empty()
        || analysis.field_examples.common_titles.is_empty()
        || analysis.field_examples.title_style.is_empty()
        || analysis.field_examples.text_style.is_empty()
        || analysis.field_examples.diagram_style.is_empty()
        || analysis.field_examples.writing_guidelines.is_empty()
        || analysis.example_document.trim().chars().count() < 100
    {
        return Err(ApiError::Upstream {
            status: StatusCode::BAD_GATEWAY,
            code: "INCOMPLETE_CREATION_SKILL_ANALYSIS",
            message: "本地模型没有生成完整的技能内容".to_string(),
        });
    }
    Ok(())
}

fn valid_distinctive_sections(sections: &[CreationSkillDistinctiveSection]) -> bool {
    sections.len() <= 6
        && sections.iter().all(|section| {
            let title_len = section.title.trim().chars().count();
            let description_len = section.description.trim().chars().count();
            let guidance_len = section.guidance.trim().chars().count();
            title_len > 0
                && title_len <= 80
                && description_len > 0
                && description_len <= 1_200
                && guidance_len > 0
                && guidance_len <= 1_200
                && !section.examples.is_empty()
                && section.examples.len() <= 6
                && section.examples.iter().all(|example| {
                    let length = example.trim().chars().count();
                    length > 0 && length <= 800
                })
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
    };

    use crate::storage::StorageManager;

    async fn spawn_delayed_creation_skill_analyzer(delay: Duration) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let body = serde_json::json!({
            "title": "技术方案写作法",
            "summary": "用于把目标、约束和验证路径组织成完整技术方案。",
            "common_titles": ["背景与目标", "方案如何落到执行"],
            "title_style": "标题直接说明章节角色。",
            "text_style": "先界定范围，再说明方案与验证。",
            "diagram_style": "仅在关系复杂时使用流程图。",
            "section_headings": CreationSkillSectionHeadings::default(),
            "field_examples": CreationSkillFieldExamples::default(),
            "example_document": "示例正文".repeat(30),
            "analysis_mode": "local_model"
        })
        .to_string();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = [0_u8; 4096];
            let _ = stream.read(&mut request).await;
            tokio::time::sleep(delay).await;
            let response = format!(
                "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).await.unwrap();
            let _ = stream.shutdown().await;
        });
        format!("http://{address}")
    }

    #[tokio::test]
    async fn analysis_job_returns_before_slow_model_and_keeps_result_for_polling() {
        let temp = tempfile::tempdir().unwrap();
        let storage = StorageManager::open(&temp.path().join("test.db")).unwrap();
        let creation_sidecar_url =
            spawn_delayed_creation_skill_analyzer(Duration::from_millis(80)).await;
        let state = AppState::with_service_urls(
            storage,
            "http://127.0.0.1:7071".to_string(),
            creation_sidecar_url,
            vec![],
        );
        let request = AnalyzeCreationSkillRequest {
            source_kind: "bake_document".to_string(),
            source_id: "document-1".to_string(),
            document_title: "示例技术方案".to_string(),
            document_content: "# 背景与目标\n说明目标与约束。\n## 方案设计\n说明实施和验证路径。"
                .to_string(),
            doc_type: "技术方案".to_string(),
        };

        let created = create_creation_skill_analysis_job(State(state.clone()), Json(request))
            .await
            .unwrap()
            .0;
        assert_eq!(created.status, "pending");

        let initial =
            get_creation_skill_analysis_job(State(state.clone()), Path(created.job_id.clone()))
                .await
                .unwrap()
                .0;
        assert!(matches!(initial.status.as_str(), "pending" | "running"));

        let deadline = Instant::now() + Duration::from_secs(3);
        let completed = loop {
            let status =
                get_creation_skill_analysis_job(State(state.clone()), Path(created.job_id.clone()))
                    .await
                    .unwrap()
                    .0;
            if status.status == "succeeded" || status.status == "failed" {
                break status;
            }
            assert!(Instant::now() < deadline, "技能分析任务未在测试时限内完成");
            tokio::time::sleep(Duration::from_millis(20)).await;
        };
        assert_eq!(completed.status, "succeeded");
        assert_eq!(
            completed
                .result
                .and_then(|value| value.get("analysis_mode").cloned()),
            Some(serde_json::Value::String("local_model".to_string()))
        );
    }

    #[test]
    fn only_allows_known_document_sources() {
        assert!(validate_source("creation_history", "1").is_ok());
        assert!(validate_source("bake_document", "2").is_ok());
        assert!(validate_source("market", "3").is_err());
        assert!(validate_source("capture", "3").is_err());
        assert!(validate_persisted_source("market", "3").is_ok());
        assert!(validate_persisted_source("manual", "manual-1").is_ok());
    }

    #[test]
    fn limits_each_skill_step_to_four_agent_and_tool_resources() {
        let mut step = CreationSkillExecutionStep {
            id: "research".into(),
            title: "开展调研".into(),
            objective: "收集并分析证据。".into(),
            output: "带来源的结论".into(),
            agents: vec![
                "industry_research_agent".into(),
                "data_analysis_agent".into(),
                "solution_design_agent".into(),
            ],
            skills: vec![],
            tools: vec!["memory_search".into(), "internet_search".into()],
            retain_webpage_screenshot: true,
        };
        assert!(!valid_execution_steps(&[step.clone()]));
        step.agents.pop();
        assert!(valid_execution_steps(&[step]));
    }

    #[test]
    fn rejects_published_skill_without_cloud_reference() {
        let skill = UpsertCreationSkill {
            client_skill_key: "local-1".into(),
            cloud_skill_id: None,
            source_kind: "creation_history".into(),
            source_id: "42".into(),
            title: "架构文档写作法".into(),
            summary: "沉淀架构设计的写作方式。".into(),
            category_id: Some("leaf".into()),
            common_titles: vec!["总体架构设计".into()],
            title_style: "结论先行。".into(),
            text_style: "正式、克制。".into(),
            diagram_style: "标注系统边界。".into(),
            writing_guidelines: vec![],
            distinctive_sections: vec![],
            section_headings: CreationSkillSectionHeadings::default(),
            field_examples: CreationSkillFieldExamples::default(),
            example_document: "# 示例服务架构设计\n\n## 背景与目标\n\n为通用知识服务明确系统边界和演进目标。\n\n## 总体架构\n\n系统划分为接入、服务和数据三层，各层通过稳定接口协作。\n\n## 实施与验证\n\n先验证关键链路，再逐步扩展能力，并用可观测指标检查结果。\n\n## 风险与结论\n\n重点关注依赖失效和数据一致性风险，所有示例均使用虚构场景。".into(),
            skill_description: CreationSkillDescription::default(),
            execution_steps: vec![],
            package_files: vec![],
            status: "saved".into(),
            installed: false,
            published: true,
        };
        assert!(validate_skill_input(&skill).is_err());
    }

    #[test]
    fn accepts_codex_skill_package_with_required_frontmatter() {
        let markdown = "---\nname: review-notes\ndescription: Review meeting notes and extract actions.\n---\n\n# Workflow\n\nRead the notes and list decisions.";
        let mut skill = UpsertCreationSkill {
            client_skill_key: "imported-review-notes".into(),
            cloud_skill_id: None,
            source_kind: "imported".into(),
            source_id: "review-notes".into(),
            title: "review-notes".into(),
            summary: "Review meeting notes and extract actions.".into(),
            category_id: None,
            common_titles: vec!["review-notes".into()],
            title_style: "Follow SKILL.md.".into(),
            text_style: "Follow SKILL.md.".into(),
            diagram_style: "Follow SKILL.md.".into(),
            writing_guidelines: vec!["Follow the package instructions.".into()],
            distinctive_sections: vec![CreationSkillDistinctiveSection {
                title: "Definition first".into(),
                description: "Explain the reusable object before the workflow.".into(),
                guidance: "Introduce its role and boundary before later actions.".into(),
                examples: vec![
                    "A workspace connects a request, its owner, and its evidence.".into(),
                ],
            }],
            section_headings: CreationSkillSectionHeadings::default(),
            field_examples: CreationSkillFieldExamples::default(),
            example_document: default_analysis_example_document(),
            skill_description: CreationSkillDescription::default(),
            execution_steps: vec![],
            package_files: vec![
                crate::storage::repo::creation_skill::CreationSkillPackageFile {
                    path: "SKILL.md".into(),
                    media_type: "text/markdown".into(),
                    content_base64: BASE64_STANDARD.encode(markdown),
                    size_bytes: markdown.len() as u64,
                },
            ],
            status: "saved".into(),
            installed: true,
            published: false,
        };

        assert!(validate_skill_input(&skill).is_ok());
        skill.distinctive_sections[0].examples.clear();
        assert!(validate_skill_input(&skill).is_err());
        skill.distinctive_sections[0].examples =
            vec!["A workspace connects a request, its owner, and its evidence.".into()];
        skill.package_files[0].path = "../SKILL.md".into();
        assert!(validate_skill_input(&skill).is_err());
        skill.package_files[0].path = "SKILL.md".into();
        skill.source_id = "other-name".into();
        assert!(validate_skill_input(&skill).is_err());
        assert!(validate_skill_markdown(
            "---\nname: review-notes\ndescription: Missing closing delimiter."
        )
        .is_err());
    }
}
