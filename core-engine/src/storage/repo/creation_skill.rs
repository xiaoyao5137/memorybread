use rusqlite::{params, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::storage::{db::current_ts_ms, StorageError, StorageManager};

const SELECT_COLUMNS: &str =
    "id, client_skill_key, cloud_skill_id, source_kind, source_id, title, summary,
     category_id, common_titles, title_style, text_style, diagram_style,
     writing_guidelines, distinctive_sections, section_headings, field_examples,
     example_document, skill_description, execution_steps, package_files,
     status, installed, published, created_at, updated_at";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CreationSkillSectionHeadings {
    #[serde(default = "default_common_titles_heading")]
    pub common_titles: String,
    #[serde(default = "default_title_style_heading")]
    pub title_style: String,
    #[serde(default = "default_text_style_heading")]
    pub text_style: String,
    #[serde(default = "default_diagram_style_heading")]
    pub diagram_style: String,
    #[serde(default = "default_writing_guidelines_heading")]
    pub writing_guidelines: String,
}

impl Default for CreationSkillSectionHeadings {
    fn default() -> Self {
        Self {
            common_titles: default_common_titles_heading(),
            title_style: default_title_style_heading(),
            text_style: default_text_style_heading(),
            diagram_style: default_diagram_style_heading(),
            writing_guidelines: default_writing_guidelines_heading(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CreationSkillFieldExamples {
    #[serde(default = "default_common_title_examples")]
    pub common_titles: Vec<String>,
    #[serde(default = "default_title_style_examples")]
    pub title_style: Vec<String>,
    #[serde(default = "default_text_style_examples")]
    pub text_style: Vec<String>,
    #[serde(default = "default_diagram_style_examples")]
    pub diagram_style: Vec<String>,
    #[serde(default = "default_writing_guideline_examples")]
    pub writing_guidelines: Vec<String>,
}

impl Default for CreationSkillFieldExamples {
    fn default() -> Self {
        Self {
            common_titles: default_common_title_examples(),
            title_style: default_title_style_examples(),
            text_style: default_text_style_examples(),
            diagram_style: default_diagram_style_examples(),
            writing_guidelines: default_writing_guideline_examples(),
        }
    }
}

fn default_common_titles_heading() -> String {
    "标题设计风格".to_string()
}

fn default_title_style_heading() -> String {
    "标题设计风格".to_string()
}

fn default_text_style_heading() -> String {
    "行文设计思路".to_string()
}

fn default_diagram_style_heading() -> String {
    "图片生成方式".to_string()
}

fn default_writing_guidelines_heading() -> String {
    "话术表达风格".to_string()
}

fn default_common_title_examples() -> Vec<String> {
    vec!["现状与约束".to_string(), "方案如何落到执行".to_string()]
}

fn default_title_style_examples() -> Vec<String> {
    default_common_title_examples()
}

fn default_text_style_examples() -> Vec<String> {
    vec!["先界定适用范围，再沿“现状 → 判断 → 动作 → 验证”逐层收束。".to_string()]
}

fn default_diagram_style_examples() -> Vec<String> {
    vec!["PlantUML 活动图：主流程纵向排列，跨角色动作放入对应泳道。".to_string()]
}

fn default_writing_guideline_examples() -> Vec<String> {
    vec!["需要说明的是，目标对象只覆盖已经确认的适用范围。".to_string()]
}

fn default_example_document() -> String {
    "# 跨团队知识交接优化方案\n\n## 摘要\n\n本示例围绕通用的知识交接场景，说明如何明确范围、责任角色、执行步骤与验收方式。\n\n## 背景与目标\n\n相关团队需要在任务变化时稳定传递必要信息，目标是减少遗漏并让接手者能够独立完成后续工作。\n\n## 方案设计\n\n建立“准备、讲解、确认、复核”四个阶段；每个阶段明确输入、责任角色、输出和完成标准。\n\n## 风险与验证\n\n重点检查资料缺失、理解偏差和权限不当三类风险，并以清单完成情况作为验收依据。".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CreationSkillPackageFile {
    pub path: String,
    pub media_type: String,
    pub content_base64: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct CreationSkillDistinctiveSection {
    pub title: String,
    pub description: String,
    pub guidance: String,
    #[serde(default)]
    pub examples: Vec<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq, Eq)]
pub struct CreationSkillDescription {
    #[serde(default)]
    pub purpose: String,
    #[serde(default)]
    pub document_types: Vec<String>,
    #[serde(default)]
    pub problems: Vec<String>,
    #[serde(default)]
    pub domains: Vec<String>,
    #[serde(default)]
    pub deliverables: Vec<String>,
}

fn default_retain_webpage_screenshot() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CreationSkillExecutionStep {
    pub id: String,
    pub title: String,
    pub objective: String,
    pub output: String,
    #[serde(default)]
    pub agents: Vec<String>,
    #[serde(default)]
    pub skills: Vec<String>,
    #[serde(default)]
    pub tools: Vec<String>,
    /// 网页数据始终优先走 AX/DOM；该开关只控制是否额外保留页面截图证据。
    #[serde(default = "default_retain_webpage_screenshot")]
    pub retain_webpage_screenshot: bool,
}

impl Default for CreationSkillExecutionStep {
    fn default() -> Self {
        Self {
            id: String::new(),
            title: String::new(),
            objective: String::new(),
            output: String::new(),
            agents: Vec::new(),
            skills: Vec::new(),
            tools: Vec::new(),
            retain_webpage_screenshot: true,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CreationSkillRecord {
    pub id: i64,
    pub client_skill_key: String,
    pub cloud_skill_id: Option<String>,
    pub source_kind: String,
    pub source_id: String,
    pub title: String,
    pub summary: String,
    pub category_id: Option<String>,
    pub common_titles: Vec<String>,
    pub title_style: String,
    pub text_style: String,
    pub diagram_style: String,
    pub writing_guidelines: Vec<String>,
    #[serde(default)]
    pub distinctive_sections: Vec<CreationSkillDistinctiveSection>,
    pub section_headings: CreationSkillSectionHeadings,
    pub field_examples: CreationSkillFieldExamples,
    pub example_document: String,
    #[serde(default)]
    pub skill_description: CreationSkillDescription,
    #[serde(default)]
    pub execution_steps: Vec<CreationSkillExecutionStep>,
    #[serde(default)]
    pub package_files: Vec<CreationSkillPackageFile>,
    pub status: String,
    pub installed: bool,
    pub published: bool,
    pub created_at: i64,
    pub updated_at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpsertCreationSkill {
    pub client_skill_key: String,
    pub cloud_skill_id: Option<String>,
    pub source_kind: String,
    pub source_id: String,
    pub title: String,
    pub summary: String,
    pub category_id: Option<String>,
    pub common_titles: Vec<String>,
    pub title_style: String,
    pub text_style: String,
    pub diagram_style: String,
    pub writing_guidelines: Vec<String>,
    #[serde(default)]
    pub distinctive_sections: Vec<CreationSkillDistinctiveSection>,
    #[serde(default)]
    pub section_headings: CreationSkillSectionHeadings,
    #[serde(default)]
    pub field_examples: CreationSkillFieldExamples,
    #[serde(default = "default_example_document")]
    pub example_document: String,
    #[serde(default)]
    pub skill_description: CreationSkillDescription,
    #[serde(default)]
    pub execution_steps: Vec<CreationSkillExecutionStep>,
    #[serde(default)]
    pub package_files: Vec<CreationSkillPackageFile>,
    pub status: String,
    pub installed: bool,
    pub published: bool,
}

impl StorageManager {
    pub fn list_creation_skills(&self) -> Result<Vec<CreationSkillRecord>, StorageError> {
        self.list_creation_skills_filtered(None, None, None)
    }

    pub fn list_creation_skills_filtered(
        &self,
        source_kind: Option<&str>,
        source_id: Option<&str>,
        installed: Option<bool>,
    ) -> Result<Vec<CreationSkillRecord>, StorageError> {
        self.with_conn(|conn| {
            let mut stmt = conn.prepare(&format!(
                "SELECT {SELECT_COLUMNS} FROM creation_skills
                 WHERE deleted_at IS NULL
                   AND (?1 IS NULL OR source_kind = ?1)
                   AND (?2 IS NULL OR source_id = ?2)
                   AND (?3 IS NULL OR installed = ?3)
                 ORDER BY updated_at DESC, id DESC"
            ))?;
            let installed_value = installed.map(i64::from);
            let rows = stmt.query_map(
                params![source_kind, source_id, installed_value],
                row_to_skill,
            )?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(StorageError::Sqlite)
        })
    }

    pub fn get_creation_skill(&self, id: i64) -> Result<Option<CreationSkillRecord>, StorageError> {
        self.with_conn(|conn| {
            conn.query_row(
                &format!(
                    "SELECT {SELECT_COLUMNS} FROM creation_skills
                     WHERE id = ?1 AND deleted_at IS NULL"
                ),
                params![id],
                row_to_skill,
            )
            .optional()
            .map_err(StorageError::Sqlite)
        })
    }

    pub fn upsert_creation_skill(
        &self,
        skill: &UpsertCreationSkill,
    ) -> Result<CreationSkillRecord, StorageError> {
        validate_skill(skill)?;
        let now = current_ts_ms();
        let common_titles = serde_json::to_string(&skill.common_titles)?;
        let writing_guidelines = serde_json::to_string(&skill.writing_guidelines)?;
        let distinctive_sections = serde_json::to_string(&skill.distinctive_sections)?;
        let section_headings = serde_json::to_string(&skill.section_headings)?;
        let field_examples = serde_json::to_string(&skill.field_examples)?;
        let skill_description = serde_json::to_string(&skill.skill_description)?;
        let execution_steps = serde_json::to_string(&skill.execution_steps)?;
        let package_files = serde_json::to_string(&skill.package_files)?;
        self.with_conn(|conn| {
            conn.execute(
                "INSERT INTO creation_skills (
                    client_skill_key, cloud_skill_id, source_kind, source_id, title, summary,
                    category_id, common_titles, title_style, text_style, diagram_style,
                    writing_guidelines, distinctive_sections, section_headings,
                    field_examples, example_document, skill_description, execution_steps,
                    package_files, status, installed, published, created_at, updated_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23, ?23)
                 ON CONFLICT(client_skill_key) DO UPDATE SET
                    cloud_skill_id = excluded.cloud_skill_id,
                    source_kind = excluded.source_kind,
                    source_id = excluded.source_id,
                    title = excluded.title,
                    summary = excluded.summary,
                    category_id = excluded.category_id,
                    common_titles = excluded.common_titles,
                    title_style = excluded.title_style,
                    text_style = excluded.text_style,
                    diagram_style = excluded.diagram_style,
                    writing_guidelines = excluded.writing_guidelines,
                    distinctive_sections = excluded.distinctive_sections,
                    section_headings = excluded.section_headings,
                    field_examples = excluded.field_examples,
                    example_document = excluded.example_document,
                    skill_description = excluded.skill_description,
                    execution_steps = excluded.execution_steps,
                    package_files = excluded.package_files,
                    status = excluded.status,
                    installed = excluded.installed,
                    published = excluded.published,
                    updated_at = excluded.updated_at,
                    deleted_at = NULL",
                params![
                    skill.client_skill_key,
                    skill.cloud_skill_id,
                    skill.source_kind,
                    skill.source_id,
                    skill.title,
                    skill.summary,
                    skill.category_id,
                    common_titles,
                    skill.title_style,
                    skill.text_style,
                    skill.diagram_style,
                    writing_guidelines,
                    distinctive_sections,
                    section_headings,
                    field_examples,
                    skill.example_document,
                    skill_description,
                    execution_steps,
                    package_files,
                    skill.status,
                    i64::from(skill.installed),
                    i64::from(skill.published),
                    now,
                ],
            )?;
            conn.query_row(
                &format!(
                    "SELECT {SELECT_COLUMNS} FROM creation_skills WHERE client_skill_key = ?1"
                ),
                params![skill.client_skill_key],
                row_to_skill,
            )
            .map_err(StorageError::Sqlite)
        })
    }

    pub fn delete_creation_skill(&self, id: i64) -> Result<bool, StorageError> {
        let now = current_ts_ms();
        self.with_conn(|conn| {
            Ok(conn.execute(
                "UPDATE creation_skills SET deleted_at = ?1, updated_at = ?1
                 WHERE id = ?2 AND deleted_at IS NULL",
                params![now, id],
            )? > 0)
        })
    }
}

// 创作配方各字段与完整示例文档在界面上允许留空，存储层只兜底核心字段；
// 长度与取值上限由 API 层 validate_skill_input 负责。
fn validate_skill(skill: &UpsertCreationSkill) -> Result<(), StorageError> {
    if skill.client_skill_key.trim().is_empty()
        || !matches!(
            skill.source_kind.as_str(),
            "creation_history" | "bake_document" | "market" | "imported" | "manual"
        )
        || skill.source_id.trim().is_empty()
        || skill.title.trim().is_empty()
        || skill.summary.trim().is_empty()
        || skill.section_headings.common_titles.trim().is_empty()
        || skill.section_headings.title_style.trim().is_empty()
        || skill.section_headings.text_style.trim().is_empty()
        || skill.section_headings.diagram_style.trim().is_empty()
        || skill.section_headings.writing_guidelines.trim().is_empty()
        || !valid_skill_description(&skill.skill_description)
        || !valid_execution_steps(&skill.execution_steps)
        || (skill.source_kind == "imported" && skill.package_files.is_empty())
        || !matches!(skill.status.as_str(), "draft" | "saved")
        || (skill.installed && skill.status != "saved")
    {
        return Err(StorageError::MigrationFailed {
            version: "creation_skill_validation",
            reason: "技能内容不完整".to_string(),
        });
    }
    Ok(())
}

fn row_to_skill(row: &rusqlite::Row<'_>) -> rusqlite::Result<CreationSkillRecord> {
    Ok(CreationSkillRecord {
        id: row.get("id")?,
        client_skill_key: row.get("client_skill_key")?,
        cloud_skill_id: row.get("cloud_skill_id")?,
        source_kind: row.get("source_kind")?,
        source_id: row.get("source_id")?,
        title: row.get("title")?,
        summary: row.get("summary")?,
        category_id: row.get("category_id")?,
        common_titles: parse_json(row.get::<_, String>("common_titles")?),
        title_style: row.get("title_style")?,
        text_style: row.get("text_style")?,
        diagram_style: row.get("diagram_style")?,
        writing_guidelines: parse_json(row.get::<_, String>("writing_guidelines")?),
        distinctive_sections: parse_json_object(row.get::<_, String>("distinctive_sections")?),
        section_headings: parse_json_object(row.get::<_, String>("section_headings")?),
        field_examples: parse_json_object(row.get::<_, String>("field_examples")?),
        example_document: {
            let value = row.get::<_, String>("example_document")?;
            if value.trim().is_empty() {
                default_example_document()
            } else {
                value
            }
        },
        skill_description: parse_json_object(row.get::<_, String>("skill_description")?),
        execution_steps: parse_json_object(row.get::<_, String>("execution_steps")?),
        package_files: parse_json_object(row.get::<_, String>("package_files")?),
        status: row.get("status")?,
        installed: row.get::<_, i64>("installed")? != 0,
        published: row.get::<_, i64>("published")? != 0,
        created_at: row.get("created_at")?,
        updated_at: row.get("updated_at")?,
    })
}

fn valid_skill_description(description: &CreationSkillDescription) -> bool {
    let legacy_empty = description.purpose.trim().is_empty()
        && description.document_types.is_empty()
        && description.problems.is_empty()
        && description.domains.is_empty()
        && description.deliverables.is_empty();
    legacy_empty
        || (!description.purpose.trim().is_empty()
            && !description.document_types.is_empty()
            && !description.problems.is_empty()
            && !description.deliverables.is_empty())
}

fn valid_execution_steps(steps: &[CreationSkillExecutionStep]) -> bool {
    steps.is_empty()
        || steps.iter().all(|step| {
            !step.id.trim().is_empty()
                && !step.title.trim().is_empty()
                && !step.objective.trim().is_empty()
                && step.agents.len() + step.tools.len() <= 4
        })
}

fn parse_json(value: String) -> Vec<String> {
    serde_json::from_str(&value).unwrap_or_default()
}

fn parse_json_object<T>(value: String) -> T
where
    T: serde::de::DeserializeOwned + Default,
{
    serde_json::from_str(&value).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn execution_step_defaults_to_retaining_webpage_screenshot() {
        let legacy: CreationSkillExecutionStep = serde_json::from_value(serde_json::json!({
            "id": "collect-data",
            "title": "采集数据",
            "objective": "读取实时页面",
            "output": "指标",
            "agents": [],
            "skills": [],
            "tools": ["data_search"]
        }))
        .unwrap();
        assert!(legacy.retain_webpage_screenshot);

        let disabled: CreationSkillExecutionStep = serde_json::from_value(serde_json::json!({
            "id": "collect-data",
            "title": "采集数据",
            "objective": "读取实时页面",
            "output": "指标",
            "retain_webpage_screenshot": false
        }))
        .unwrap();
        assert!(!disabled.retain_webpage_screenshot);
    }

    fn sample_skill() -> UpsertCreationSkill {
        UpsertCreationSkill {
            client_skill_key: "skill-local-1".into(),
            cloud_skill_id: None,
            source_kind: "creation_history".into(),
            source_id: "12".into(),
            title: "架构文档 Skill".into(),
            summary: "复用架构文档的写作方式。".into(),
            category_id: Some("category-1".into()),
            common_titles: vec!["总体架构设计".into()],
            title_style: "结论先行。".into(),
            text_style: "正式、克制。".into(),
            diagram_style: "分层架构图。".into(),
            writing_guidelines: vec!["说明取舍。".into()],
            distinctive_sections: vec![CreationSkillDistinctiveSection {
                title: "定义先行".into(),
                description: "先建立共同概念，再进入方案展开。".into(),
                guidance: "在首次出现核心对象时，用一句通俗解释和一句边界说明完成定义。".into(),
                examples: vec!["协作工作台可以理解为连接任务、角色与结果证据的统一入口。".into()],
            }],
            section_headings: CreationSkillSectionHeadings::default(),
            field_examples: CreationSkillFieldExamples::default(),
            example_document: default_example_document(),
            skill_description: CreationSkillDescription {
                purpose: "用于把已确认的技术事实组织成可评审、可实施的架构文档。".into(),
                document_types: vec!["技术架构设计文档".into()],
                problems: vec!["澄清系统边界、关键取舍和实施路径".into()],
                domains: vec!["软件架构".into()],
                deliverables: vec!["包含架构、链路、风险和验证方式的 Markdown 文档".into()],
            },
            execution_steps: vec![CreationSkillExecutionStep {
                id: "design-solution".into(),
                title: "设计总体方案".into(),
                objective: "把约束和证据转化为结构化架构方案。".into(),
                output: "总体方案与关键设计".into(),
                agents: vec!["solution_design_agent".into()],
                skills: vec![],
                tools: vec!["plantuml_diagram".into()],
                retain_webpage_screenshot: true,
            }],
            package_files: vec![],
            status: "saved".into(),
            installed: false,
            published: false,
        }
    }

    #[test]
    fn local_skill_upsert_is_idempotent() {
        let storage = StorageManager::open_in_memory().unwrap();
        let first = storage.upsert_creation_skill(&sample_skill()).unwrap();
        let mut updated = sample_skill();
        updated.title = "更新后的架构文档 Skill".into();
        let second = storage.upsert_creation_skill(&updated).unwrap();
        assert_eq!(first.id, second.id);
        assert_eq!(second.title, "更新后的架构文档 Skill");
        assert_eq!(second.distinctive_sections.len(), 1);
        assert_eq!(second.distinctive_sections[0].title, "定义先行");
        assert_eq!(
            second.skill_description.document_types,
            vec!["技术架构设计文档"]
        );
        assert_eq!(second.execution_steps[0].id, "design-solution");
        assert_eq!(
            second.execution_steps[0].agents,
            vec!["solution_design_agent"]
        );
        assert_eq!(storage.list_creation_skills().unwrap().len(), 1);
    }

    #[test]
    fn stores_skill_with_empty_recipe_and_example_document() {
        let storage = StorageManager::open_in_memory().unwrap();
        let mut minimal = sample_skill();
        minimal.common_titles.clear();
        minimal.title_style.clear();
        minimal.text_style.clear();
        minimal.diagram_style.clear();
        minimal.writing_guidelines.clear();
        minimal.field_examples = CreationSkillFieldExamples {
            common_titles: vec![],
            title_style: vec![],
            text_style: vec![],
            diagram_style: vec![],
            writing_guidelines: vec![],
        };
        minimal.example_document.clear();

        let saved = storage.upsert_creation_skill(&minimal).unwrap();
        assert_eq!(saved.title, minimal.title);
        assert_eq!(saved.common_titles, Vec::<String>::new());
    }

    #[test]
    fn filters_skills_by_source_and_installation() {
        let storage = StorageManager::open_in_memory().unwrap();
        let mut installed = sample_skill();
        installed.installed = true;
        storage.upsert_creation_skill(&installed).unwrap();

        let by_source = storage
            .list_creation_skills_filtered(Some("creation_history"), Some("12"), None)
            .unwrap();
        let installed_only = storage
            .list_creation_skills_filtered(None, None, Some(true))
            .unwrap();

        assert_eq!(by_source.len(), 1);
        assert_eq!(installed_only.len(), 1);
        assert!(installed_only[0].installed);
    }

    #[test]
    fn stores_market_skill_as_an_installed_local_copy() {
        let storage = StorageManager::open_in_memory().unwrap();
        let mut market = sample_skill();
        market.client_skill_key = "market-01900000-0000-7000-8000-000000000001".into();
        market.cloud_skill_id = Some("01900000-0000-7000-8000-000000000001".into());
        market.source_kind = "market".into();
        market.source_id = "01900000-0000-7000-8000-000000000001".into();
        market.installed = true;

        let saved = storage.upsert_creation_skill(&market).unwrap();

        assert_eq!(saved.source_kind, "market");
        assert!(saved.installed);
        assert!(!saved.published);
    }

    #[test]
    fn stores_imported_codex_skill_files() {
        let storage = StorageManager::open_in_memory().unwrap();
        let mut imported = sample_skill();
        imported.client_skill_key = "imported-review-notes".into();
        imported.source_kind = "imported".into();
        imported.source_id = "review-notes".into();
        imported.package_files = vec![CreationSkillPackageFile {
            path: "SKILL.md".into(),
            media_type: "text/markdown".into(),
            content_base64: "IyBTa2lsbA==".into(),
            size_bytes: 7,
        }];

        let saved = storage.upsert_creation_skill(&imported).unwrap();

        assert_eq!(saved.source_kind, "imported");
        assert_eq!(saved.package_files, imported.package_files);
    }
}
