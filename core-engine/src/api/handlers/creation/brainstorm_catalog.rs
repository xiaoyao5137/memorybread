//! Installed skill packages are the authority for static brainstorm methods.
use super::{brainstorm_stages::candidate_copy_is_complete, AppState};
use crate::storage::repo::creation_skill::{
    CreationSkillDescription, CreationSkillExecutionStep, CreationSkillRecord,
};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};

const CATALOG_PATH: &str = "references/brainstorm-catalog.json";
const PROFILE_PATH: &str = "references/memorybread-creation.json";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct TrustedCatalog {
    pub skill_key: String,
    pub profile_sha256: String,
    pub catalog_sha256: String,
    pub catalog: Catalog,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct Catalog {
    pub schema_version: u32,
    pub kind: String,
    pub catalog_id: String,
    pub catalog_version: u32,
    pub skill_key: String,
    pub skill_profile_sha256: String,
    pub stages: BTreeMap<String, CatalogStage>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct CatalogStage {
    pub topics: Vec<CatalogTopic>,
    pub slots: Vec<CatalogSlot>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct CatalogTopic {
    pub id: String,
    pub title: String,
    pub goal: String,
    pub root_slot_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct CatalogSlot {
    pub id: String,
    pub topic_id: String,
    pub object_id: String,
    pub choice_axis: String,
    pub required_fields: Vec<String>,
    pub prompt_body: String,
    pub why_now: String,
    pub methods: Vec<CatalogMethod>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct CatalogMethod {
    pub id: String,
    pub object_id: String,
    pub required_fields: Vec<String>,
    pub label: String,
    pub description: String,
    pub tradeoff: String,
    pub child_slot_ids: Vec<String>,
}

pub(super) fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn text(value: &str, maximum: usize) -> bool {
    value == value.trim()
        && !value.is_empty()
        && value.chars().count() <= maximum
        && candidate_copy_is_complete(value)
}

fn fields(values: &[String]) -> bool {
    (1..=32).contains(&values.len())
        && values.iter().all(|value| identifier(value))
        && values.iter().collect::<HashSet<_>>().len() == values.len()
}

impl TrustedCatalog {
    pub fn stage(&self, stage: &str) -> Option<&CatalogStage> {
        self.catalog.stages.get(stage)
    }
    pub fn slot(&self, stage: &str, goal: &str) -> Option<&CatalogSlot> {
        let id = goal.strip_prefix("catalog_slot:")?;
        self.stage(stage)?.slots.iter().find(|slot| slot.id == id)
    }
    pub fn subtree_size(&self, stage: &str, goal: &str) -> Option<usize> {
        let slot = self.slot(stage, goal)?;
        slot.methods
            .iter()
            .flat_map(|method| &method.child_slot_ids)
            .try_fold(1usize, |count, id| {
                count.checked_add(self.subtree_size(stage, &slot_goal(id))?)
            })
    }
}

pub(super) fn slot_goal(id: &str) -> String {
    format!("catalog_slot:{id}")
}

pub(super) fn validate(
    catalog: &Catalog,
    max_topics: usize,
    max_questions: usize,
    max_depth: usize,
) -> Result<(), ()> {
    if catalog.schema_version != 1
        || catalog.kind != "memorybread.brainstorm-catalog"
        || !identifier(&catalog.catalog_id)
        || catalog.catalog_version == 0
        || !identifier(&catalog.skill_key)
        || catalog.skill_profile_sha256.len() != 64
        || !catalog
            .skill_profile_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
        || catalog.stages.is_empty()
        || catalog.stages.len() > 3
        || !(2..=3).contains(&max_depth)
    {
        return Err(());
    }
    for (stage_name, stage) in &catalog.stages {
        if !matches!(stage_name.as_str(), "direction" | "detail" | "writing")
            || stage.topics.is_empty()
            || stage.topics.len() > max_topics
            || stage.slots.is_empty()
            || stage.slots.len() > 144
        {
            return Err(());
        }
        let mut topic_ids = HashSet::new();
        let mut slot_ids = HashSet::new();
        let mut bodies = HashSet::new();
        for topic in &stage.topics {
            if !identifier(&topic.id)
                || !topic_ids.insert(&topic.id)
                || !text(&topic.title, 60)
                || !text(&topic.goal, 200)
                || topic.root_slot_ids.is_empty()
                || topic.root_slot_ids.len() > 8
            {
                return Err(());
            }
        }
        for slot in &stage.slots {
            if !identifier(&slot.id)
                || !slot_ids.insert(&slot.id)
                || !topic_ids.contains(&slot.topic_id)
                || !identifier(&slot.object_id)
                || !text(&slot.choice_axis, 80)
                || !fields(&slot.required_fields)
                || !text(&slot.prompt_body, 60)
                || !text(&slot.why_now, 80)
                || !bodies.insert(super::brainstorm_question_fingerprint(&slot.prompt_body))
                || !(2..=5).contains(&slot.methods.len())
            {
                return Err(());
            }
            let mut method_ids = HashSet::new();
            let mut labels = HashSet::new();
            let mut meanings = HashSet::new();
            for method in &slot.methods {
                if !identifier(&method.id)
                    || !method_ids.insert(&method.id)
                    || method.object_id != slot.object_id
                    || method.required_fields != slot.required_fields
                    || !text(&method.label, 24)
                    || !text(&method.description, 80)
                    || !text(&method.tradeoff, 160)
                    || !labels.insert(super::brainstorm_question_fingerprint(&method.label))
                    || !meanings.insert(super::brainstorm_question_fingerprint(&format!(
                        "{} {}",
                        method.description, method.tradeoff
                    )))
                    || method.child_slot_ids.len() > 8
                {
                    return Err(());
                }
            }
        }
        let slots: BTreeMap<_, _> = stage
            .slots
            .iter()
            .map(|slot| (slot.id.as_str(), slot))
            .collect();
        let mut visited = HashSet::new();
        for topic in &stage.topics {
            let mut queue: Vec<_> = topic
                .root_slot_ids
                .iter()
                .map(|id| (id.as_str(), 1usize, String::new()))
                .collect();
            let mut count = 0;
            while let Some((id, depth, prefix)) = queue.pop() {
                let slot = slots.get(id).ok_or(())?;
                if !visited.insert(id)
                    || slot.topic_id != topic.id
                    || depth > max_depth
                    || prefix.chars().count() + slot.prompt_body.chars().count() > 60
                {
                    return Err(());
                }
                count += 1;
                for method in &slot.methods {
                    if depth == max_depth && !method.child_slot_ids.is_empty() {
                        return Err(());
                    }
                    let condition = if prefix.is_empty() {
                        format!("若「{}」，", method.label)
                    } else {
                        format!("{}且「{}」，", prefix.trim_end_matches('，'), method.label)
                    };
                    queue.extend(
                        method
                            .child_slot_ids
                            .iter()
                            .map(|id| (id.as_str(), depth + 1, condition.clone())),
                    );
                }
            }
            if count > max_questions {
                return Err(());
            }
        }
        if visited.len() != stage.slots.len() {
            return Err(());
        }
    }
    Ok(())
}

fn package_bytes(record: &CreationSkillRecord, path: &str) -> Result<Option<Vec<u8>>, ()> {
    let files: Vec<_> = record
        .package_files
        .iter()
        .filter(|file| file.path == path)
        .collect();
    if files.is_empty() {
        return Ok(None);
    }
    if files.len() != 1 || files[0].size_bytes > 262_144 {
        return Err(());
    }
    let bytes = STANDARD.decode(&files[0].content_base64).map_err(|_| ())?;
    if bytes.len() as u64 != files[0].size_bytes {
        return Err(());
    }
    Ok(Some(bytes))
}

fn semantic(value: Value) -> Result<Value, ()> {
    match value {
        Value::Array(items) => Ok(Value::Array(
            items.into_iter().map(semantic).collect::<Result<_, _>>()?,
        )),
        Value::Object(items) => {
            let mut result = serde_json::Map::new();
            for (key, value) in items {
                let key = match key.as_str() {
                    "documentTypes" => "document_types",
                    "metadataReview" => "metadata_review",
                    "outputRole" => "output_role",
                    "retainWebpageScreenshot" => "retain_webpage_screenshot",
                    _ => &key,
                }
                .to_owned();
                if result.insert(key, semantic(value)?).is_some() {
                    return Err(());
                }
            }
            result.remove("metadata_review");
            if ["id", "objective", "agents"]
                .iter()
                .all(|key| result.contains_key(*key))
            {
                result
                    .entry("output_role")
                    .or_insert(Value::String(String::new()));
            }
            Ok(Value::Object(result))
        }
        value => Ok(value),
    }
}

pub(super) fn from_record(
    record: &CreationSkillRecord,
    max_topics: usize,
    max_questions: usize,
    max_depth: usize,
) -> Result<Option<TrustedCatalog>, ()> {
    let Some(bytes) = package_bytes(record, CATALOG_PATH)? else {
        return Ok(None);
    };
    if !record.installed || record.status != "saved" {
        return Err(());
    }
    let profile_bytes = package_bytes(record, PROFILE_PATH)?.ok_or(())?;
    let profile: Value = serde_json::from_slice(&profile_bytes).map_err(|_| ())?;
    if profile["schema_version"] != 1 || profile["kind"] != "memorybread.creation-profile" {
        return Err(());
    }
    let mut description: CreationSkillDescription =
        serde_json::from_value(profile["content"]["skill_description"].clone()).map_err(|_| ())?;
    let steps: Vec<CreationSkillExecutionStep> =
        serde_json::from_value(profile["content"]["execution_steps"].clone()).map_err(|_| ())?;
    let mut expected_description = record.skill_description.clone();
    // Metadata review is derived operational evidence, not a writing rule.
    description.metadata_review = None;
    expected_description.metadata_review = None;
    if semantic(profile["content"]["skill_description"].clone())?
        != semantic(serde_json::to_value(&expected_description).map_err(|_| ())?)?
        || semantic(profile["content"]["execution_steps"].clone())?
            != semantic(serde_json::to_value(&record.execution_steps).map_err(|_| ())?)?
        || description != expected_description
        || steps != record.execution_steps
    {
        return Err(());
    }
    let catalog: Catalog = serde_json::from_slice(&bytes).map_err(|_| ())?;
    let profile_sha256 = digest(&profile_bytes);
    if catalog.skill_key != record.client_skill_key
        || catalog.skill_profile_sha256 != profile_sha256
    {
        return Err(());
    }
    validate(&catalog, max_topics, max_questions, max_depth)?;
    Ok(Some(TrustedCatalog {
        skill_key: record.client_skill_key.clone(),
        profile_sha256,
        catalog_sha256: digest(&bytes),
        catalog,
    }))
}

pub(super) fn resolve(
    state: &AppState,
    selected: &[Value],
    stage: &str,
    max_topics: usize,
    max_questions: usize,
    max_depth: usize,
) -> Result<Option<TrustedCatalog>, ()> {
    state
        .storage
        .with_conn(|conn| {
            Ok(resolve_on_connection(
                conn,
                selected,
                stage,
                max_topics,
                max_questions,
                max_depth,
            ))
        })
        .map_err(|_| ())?
}

pub(super) fn resolve_on_connection(
    conn: &rusqlite::Connection,
    selected: &[Value],
    stage: &str,
    max_topics: usize,
    max_questions: usize,
    max_depth: usize,
) -> Result<Option<TrustedCatalog>, ()> {
    let records =
        crate::storage::repo::creation_skill::list_for_brainstorm_catalog(conn).map_err(|_| ())?;
    let mut result = None;
    let mut seen = HashSet::new();
    for selection in selected {
        let key = selection
            .get("client_skill_key")
            .or_else(|| selection.get("clientSkillKey"))
            .or_else(|| selection.get("id"));
        let record = records.iter().find(|record| {
            key.is_some_and(|key| {
                key.as_str() == Some(record.client_skill_key.as_str())
                    || key.as_i64() == Some(record.id)
            })
        });
        let Some(record) = record else {
            continue;
        };
        if !seen.insert(&record.client_skill_key) {
            continue;
        }
        if let Some(catalog) = from_record(record, max_topics, max_questions, max_depth)? {
            if catalog.stage(stage).is_some() {
                let selected_description = selection
                    .get("skillDescription")
                    .or_else(|| selection.get("skill_description"))
                    .cloned()
                    .ok_or(())?;
                let selected_steps = selection
                    .get("executionSteps")
                    .or_else(|| selection.get("execution_steps"))
                    .cloned()
                    .ok_or(())?;
                if semantic(selected_description)?
                    != semantic(serde_json::to_value(&record.skill_description).map_err(|_| ())?)?
                    || semantic(selected_steps)?
                        != semantic(serde_json::to_value(&record.execution_steps).map_err(|_| ())?)?
                {
                    return Err(());
                }
                if result.is_some() {
                    return Err(());
                }
                result = Some(catalog);
            }
        }
    }
    Ok(result)
}

#[cfg(test)]
pub(super) mod tests {
    use super::*;
    use crate::storage::{
        repo::creation_skill::{CreationSkillPackageFile, UpsertCreationSkill},
        StorageManager,
    };
    use serde_json::json;

    pub fn record() -> CreationSkillRecord {
        let description = json!({"purpose":"将已确认规则组织为可执行文档","document_types":["方案"],"problems":["组织证据"],"domains":["技术"],"deliverables":["文档"]});
        let steps = json!([{ "id":"organize","title":"组织文档","objective":"整理证据","output":"文档","agents":[],"skills":[],"tools":[],"retain_webpage_screenshot":true}]);
        let profile = serde_json::to_vec(&json!({"schema_version":1,"kind":"memorybread.creation-profile","content":{"skill_description":description,"execution_steps":steps}})).unwrap();
        let mut catalog: Value = serde_json::from_str(include_str!("../../../../../shared/creation-brainstorm/catalogs/gpu-cost-weekly-report-detail-v1.json")).unwrap();
        catalog["skill_key"] = json!("catalog-test-skill");
        catalog["skill_profile_sha256"] = json!(digest(&profile));
        let catalog = serde_json::to_vec(&catalog).unwrap();
        let files: Vec<_> = [(PROFILE_PATH, profile), (CATALOG_PATH, catalog)]
            .into_iter()
            .map(|(path, bytes)| CreationSkillPackageFile {
                path: path.into(),
                media_type: "application/json".into(),
                size_bytes: bytes.len() as u64,
                content_base64: STANDARD.encode(bytes),
            })
            .collect();
        serde_json::from_value(json!({"id":1,"client_skill_key":"catalog-test-skill","cloud_skill_id":null,"source_kind":"manual","source_id":"test","title":"可信目录测试","summary":"规则组织","category_id":null,"common_titles":[],"title_style":"","text_style":"","diagram_style":"","writing_guidelines":[],"section_headings":{},"field_examples":{},"example_document":"方案", "skill_description":description,"execution_steps":steps,"package_files":files,"status":"saved","installed":true,"published":false,"created_at":1,"updated_at":1})).unwrap()
    }
    pub fn selected(record: &CreationSkillRecord) -> Value {
        json!({"id":record.client_skill_key,"skillDescription":record.skill_description,"executionSteps":record.execution_steps})
    }
    pub fn install(storage: &StorageManager, record: &CreationSkillRecord) {
        let input: UpsertCreationSkill =
            serde_json::from_value(serde_json::to_value(record).unwrap()).unwrap();
        storage.upsert_creation_skill(&input).unwrap();
    }
    fn rewrite(record: &mut CreationSkillRecord, change: impl FnOnce(&mut Value)) {
        let file = record
            .package_files
            .iter_mut()
            .find(|f| f.path == CATALOG_PATH)
            .unwrap();
        let mut value: Value =
            serde_json::from_slice(&STANDARD.decode(&file.content_base64).unwrap()).unwrap();
        change(&mut value);
        let bytes = serde_json::to_vec(&value).unwrap();
        file.size_bytes = bytes.len() as u64;
        file.content_base64 = STANDARD.encode(bytes);
    }
    #[test]
    fn canonical_package_and_same_version_raw_hash_bind_semantic_profile() {
        let original = record();
        let binding = from_record(&original, 8, 24, 3).unwrap().unwrap();
        assert_eq!(binding.stage("detail").unwrap().slots.len(), 15);
        assert_eq!(
            binding.subtree_size(
                "detail",
                &slot_goal(&binding.stage("detail").unwrap().topics[0].root_slot_ids[0])
            ),
            Some(3)
        );
        let mut changed = original.clone();
        rewrite(&mut changed, |v| {
            v["stages"]["detail"]["topics"][0]["goal"] = json!("另一个完整目标")
        });
        let newer = from_record(&changed, 8, 24, 3).unwrap().unwrap();
        assert_eq!(
            binding.catalog.catalog_version,
            newer.catalog.catalog_version
        );
        assert_ne!(binding.catalog_sha256, newer.catalog_sha256);
        changed = original.clone();
        changed.skill_description.purpose = "不一致的规范规则".into();
        assert!(from_record(&changed, 8, 24, 3).is_err());
        for (installed, status) in [(false, "saved"), (true, "draft")] {
            changed = original.clone();
            changed.installed = installed;
            changed.status = status.into();
            assert!(from_record(&changed, 8, 24, 3).is_err());
        }
        let mut no_catalog = original;
        no_catalog.package_files.retain(|f| f.path != CATALOG_PATH);
        assert!(from_record(&no_catalog, 8, 24, 3).unwrap().is_none());
    }
    #[test]
    fn strict_graph_rejects_unknown_fields_cycles_reuse_wrong_object_and_budget() {
        let changes: Vec<Box<dyn Fn(&mut Value)>> = vec![
            Box::new(|v| v["unknown"] = json!(true)),
            Box::new(|v| {
                v["stages"]["detail"]["slots"][0]["methods"][0]["object_id"] = json!("wrong")
            }),
            Box::new(|v| {
                v["stages"]["detail"]["slots"][0]["methods"][0]["required_fields"] =
                    json!(["wrong"])
            }),
            Box::new(|v| {
                let id = v["stages"]["detail"]["slots"][0]["id"].clone();
                v["stages"]["detail"]["slots"][0]["methods"][0]["child_slot_ids"] = json!([id]);
            }),
            Box::new(|v| {
                let ids = v["stages"]["detail"]["slots"][0]["methods"][0]["child_slot_ids"].clone();
                v["stages"]["detail"]["slots"][0]["methods"][1]["child_slot_ids"] = ids;
            }),
            Box::new(|v| {
                v["stages"]["detail"]["slots"][0]["methods"][0]["label"] = json!("2026-08-")
            }),
        ];
        for change in changes {
            let mut row = record();
            rewrite(&mut row, change);
            assert!(from_record(&row, 8, 24, 3).is_err());
        }
        assert!(from_record(&record(), 4, 24, 3).is_err());
        assert!(from_record(&record(), 8, 2, 3).is_err());
        assert!(from_record(&record(), 8, 24, 1).is_err());
        let mut row = record();
        row.package_files.push(row.package_files[1].clone());
        assert!(from_record(&row, 8, 24, 3).is_err());
    }
    #[test]
    fn resolver_ignores_client_catalog_and_rejects_snapshot_mismatch_or_ambiguity() {
        let storage = StorageManager::open_in_memory().unwrap();
        let row = record();
        install(&storage, &row);
        let mut choice = selected(&row);
        choice["trusted_catalog"] = json!({"fake":true});
        choice["packageFiles"] = json!([]);
        let resolve = |choices: Vec<Value>| {
            storage
                .with_conn(|conn| Ok(resolve_on_connection(conn, &choices, "detail", 8, 24, 3)))
                .unwrap()
        };
        assert_eq!(
            resolve(vec![choice.clone()])
                .unwrap()
                .unwrap()
                .catalog_sha256,
            from_record(&row, 8, 24, 3).unwrap().unwrap().catalog_sha256
        );
        choice["skillDescription"]["purpose"] = json!("前端伪造规则");
        assert!(resolve(vec![choice]).is_err());
        assert!(resolve(vec![selected(&row)]).unwrap().is_some());
        assert!(storage
            .with_conn(|conn| Ok(resolve_on_connection(
                conn,
                &[selected(&row)],
                "writing",
                8,
                24,
                3
            )))
            .unwrap()
            .unwrap()
            .is_none());
        let mut second = row.clone();
        second.client_skill_key = "second-catalog".into();
        rewrite(&mut second, |v| v["skill_key"] = json!("second-catalog"));
        install(&storage, &second);
        assert!(resolve(vec![selected(&row), selected(&second)]).is_err());
    }
}
