//! Finite creative attributes. This identity does not prove free-copy semantics.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::io::Read;
use std::path::Path;

const DEFAULT_ASSET: &[u8] =
    include_bytes!("../../../../../shared/creation-brainstorm/root-axes-v1.json");
const VALUE_ASSET: &[u8] =
    include_bytes!("../../../../../shared/creation-brainstorm/attribute-values-v1.json");
const MAX_REGISTRY_BYTES: usize = 128 * 1024;
const CONFIG_ENV: &str = "MEMORYBREAD_BRAINSTORM_ROOT_AXES_PATH";
const INVALID: &str = "可信根属性目录无效、已变更或与当前配置绑定不一致";
// Configuration can select/order these capabilities and restrict stages. It
// cannot give a second identity to the same property or supply new goal prose.
const IMPLEMENTED: &[(&str, &str, &str, &str, bool)] = &[
    ("audience", "阅读对象范围", "作品面向哪些阅读对象；汇报对象、阅读对象和受众范围均属于这一属性，不预设单一对象，不包含信息粒度或概念解释深度。", "在全部既定要求内，选择当前主题尚未确定的阅读对象范围", true),
    ("purpose", "表达目的", "作品希望读者理解、判断或采取什么行动；这是表达目的，不是内容侧重、语气或行动的具体实施方法。", "在全部既定要求内，确定当前主题尚未确定的表达目的", true),
    ("content_focus", "表达侧重", "保留全部必需内容时，哪些内容在表达中获得更突出的位置与说明；不选择删去必需内容，也不改变表达目的。", "保留全部必需内容，确定当前主题尚未确定的表达侧重", true),
    ("information_granularity", "信息呈现粒度", "完整必需信息在表达中的呈现粒度；不改变必需字段、数据关系、计算规则或概念解释的深度。", "保留全部必需信息及其关系，确定当前主题尚未确定的信息呈现粒度", false),
    ("concept_explanation_depth", "概念解释深度", "对概念、术语与原理补充多少解释；不重新选择读者对象，不改变数据呈现粒度或删去必需信息。", "保留全部必需内容，确定当前主题尚未确定的概念解释深度", false),
    ("evidence_presentation", "论据呈现", "已有完整论据怎样支持表达中的判断；不选择资料权限、替换来源、删去必需论据或制造事实。", "保留全部必需论据与来源，确定当前主题尚未确定的论据呈现方式", false),
    ("narrative_perspective", "叙事视角", "叙述从哪个观察位置展开；不重新选择读者、表达目的或事件推进节奏。", "在全部既定要求内，确定当前主题尚未确定的叙事视角", true),
    ("structural_order", "新增表达单元的组织次序", "尚未安排的表达单元采用什么组织次序；保留既定顺序与完整内容，不改变事件推进节奏或重排固定数据关系。", "保留全部既定内容与安排，确定当前主题新增表达单元的组织次序", false),
    ("rhythm", "展开节奏", "当前表达怎样分配展开与收束的节奏；不改变既定内容、表达单元的组织次序或叙事视角。", "保留全部既定内容，确定当前主题尚未确定的展开节奏", false),
    ("tone", "表达语气", "表达采用怎样的语气；不重新选择受众、目的、事实结论或必需内容。", "在全部既定要求内，确定当前主题尚未确定的表达语气", true),
    ("action_plan", "行动实施方式", "在全部既定目标与要求内，行动采用什么具体实施方式；不更改目标、取消固定动作、补造资源或制造成果。", "保留全部既定目标与要求，确定当前主题尚未确定的行动实施方式", false),
];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct RootAxesBinding {
    pub config_sha256: String,
    pub config: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    schema_version: u32,
    kind: String,
    registry_id: String,
    registry_version: u32,
    value_capabilities_sha256: String,
    axes: Vec<Axis>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Axis {
    id: String,
    property: String,
    title: String,
    decision_mode: String,
    definition: String,
    goal: String,
    stages: Vec<String>,
    value_ids: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ValueCapabilities {
    schema_version: u32,
    kind: String,
    capability_version: u32,
    renderer_id: String,
    axes: Vec<ValueAxis>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ValueAxis {
    axis_id: String,
    #[serde(rename = "type")]
    question_type: String,
    single_choice_reason: String,
    prompt_body: String,
    why_now: String,
    answer_template: String,
    values: Vec<AttributeValue>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AttributeValue {
    id: String,
    label: String,
    description: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ValueSelection {
    options: Vec<SelectedValue>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SelectedValue {
    value_id: String,
    recommended: bool,
    memory_choice: String,
}

fn bounded_copy(value: &str, maximum: usize, empty: bool) -> bool {
    (empty || !value.is_empty())
        && value.trim() == value
        && value.chars().count() <= maximum
        && !value.chars().any(char::is_control)
}

fn value_capabilities(bytes: &[u8]) -> Result<ValueCapabilities, String> {
    if bytes.is_empty() || bytes.len() > MAX_REGISTRY_BYTES {
        return invalid();
    }
    // The installed capability text is the only renderer authority. The
    // configurable registry contains selectors and its exact byte hash.
    let asset: ValueCapabilities = serde_json::from_slice(bytes).map_err(|_| INVALID.to_owned())?;
    if asset.schema_version != 1
        || asset.kind != "memorybread.brainstorm-attribute-values"
        || asset.capability_version != 1
        || asset.renderer_id != "atomic_values_v1"
        || asset.axes.len() != IMPLEMENTED.len()
    {
        return invalid();
    }
    let mut axes = HashSet::new();
    let mut all_values = HashSet::new();
    for axis in &asset.axes {
        if !IMPLEMENTED.iter().any(|record| record.0 == axis.axis_id)
            || !axes.insert(&axis.axis_id)
            || !matches!(
                axis.question_type.as_str(),
                "single_choice" | "multi_choice"
            )
            || (axis.question_type == "multi_choice" && !axis.single_choice_reason.is_empty())
            || !bounded_copy(
                &axis.single_choice_reason,
                80,
                axis.question_type == "multi_choice",
            )
            || !bounded_copy(&axis.prompt_body, 60, false)
            || !bounded_copy(&axis.why_now, 80, false)
            || !bounded_copy(&axis.answer_template, 80, false)
            || !(2..=5).contains(&axis.values.len())
        {
            return invalid();
        }
        let prefix = format!("{}.", axis.axis_id);
        let mut labels = HashSet::new();
        for value in &axis.values {
            if !value.id.starts_with(&prefix)
                || value.id.len() <= prefix.len()
                || value.id.len() > 100
                || !value
                    .id
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'.')
                || !all_values.insert(&value.id)
                || !labels.insert(&value.label)
                || !bounded_copy(&value.label, 24, false)
                || !bounded_copy(&value.description, 80, false)
            {
                return invalid();
            }
        }
    }
    Ok(asset)
}

fn valid_memory_choice(choice: &str) -> bool {
    if choice == "original" {
        return true;
    }
    let Some(references) = choice.strip_prefix("reference:") else {
        return false;
    };
    let mut seen = HashSet::new();
    let items: Vec<&str> = references.split('+').collect();
    (1..=2).contains(&items.len())
        && items.iter().all(|id| {
            id.strip_prefix('m').is_some_and(|number| {
                !number.is_empty()
                    && !number.starts_with('0')
                    && number.bytes().all(|b| b.is_ascii_digit())
            }) && seen.insert(*id)
        })
}

fn invalid<T>() -> Result<T, String> {
    Err(INVALID.into())
}

fn validate_config(config: &Config) -> Result<(), String> {
    let capabilities = value_capabilities(VALUE_ASSET)?;
    if config.schema_version != 1
        || config.kind != "memorybread.brainstorm-root-axes"
        || config.registry_id != "generic-creation-attributes"
        || config.registry_version < 4
        || config.value_capabilities_sha256 != format!("{:x}", Sha256::digest(VALUE_ASSET))
        || config.axes.is_empty()
        || config.axes.len() > IMPLEMENTED.len()
    {
        return invalid();
    }
    let mut ids = HashSet::new();
    let mut properties = HashSet::new();
    let mut goals = HashSet::new();
    for axis in &config.axes {
        let Some((id, title, definition, goal, direction)) =
            IMPLEMENTED.iter().find(|(id, _, _, _, _)| *id == axis.id)
        else {
            return invalid();
        };
        let mut stages = HashSet::new();
        if axis.property != *id
            || axis.title != *title
            || axis.decision_mode != "atomic"
            || axis.definition != *definition
            || axis.goal != *goal
            || axis.goal.chars().count() > 80
            || !ids.insert(&axis.id)
            || !properties.insert(&axis.property)
            || !goals.insert(&axis.goal)
            || axis.stages.is_empty()
            || axis.stages.iter().any(|stage| {
                !stages.insert(stage)
                    || !(stage == "detail" || (*direction && stage == "direction"))
            })
        {
            return invalid();
        }
        let values = &capabilities
            .axes
            .iter()
            .find(|record| record.axis_id == axis.id)
            .ok_or_else(|| INVALID.to_owned())?
            .values;
        if !(2..=values.len()).contains(&axis.value_ids.len()) {
            return invalid();
        }
        // Selecting a subset is configurable; aliases, reordered catalog
        // records and IDs belonging to another property are not new values.
        let mut previous = None;
        for id in &axis.value_ids {
            let index = values
                .iter()
                .position(|value| &value.id == id)
                .ok_or_else(|| INVALID.to_owned())?;
            if previous.is_some_and(|position| index <= position) {
                return invalid();
            }
            previous = Some(index);
        }
    }
    Ok(())
}

fn from_bytes(bytes: &[u8]) -> Result<RootAxesBinding, String> {
    if bytes.is_empty() || bytes.len() > MAX_REGISTRY_BYTES {
        return invalid();
    }
    // Deserialize from original bytes before Value, so duplicate object fields
    // and unknown fields cannot disappear in a permissive JSON map.
    let config: Config = serde_json::from_slice(bytes).map_err(|_| INVALID.to_owned())?;
    validate_config(&config)?;
    let config: Value = serde_json::from_slice(bytes).map_err(|_| INVALID.to_owned())?;
    Ok(RootAxesBinding {
        config_sha256: format!("{:x}", Sha256::digest(bytes)),
        config,
    })
}

fn from_path(path: &Path) -> Result<RootAxesBinding, String> {
    let file = std::fs::File::open(path).map_err(|_| INVALID.to_owned())?;
    let mut bytes = Vec::new();
    file.take((MAX_REGISTRY_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| INVALID.to_owned())?;
    from_bytes(&bytes)
}

pub(super) fn load() -> Result<RootAxesBinding, String> {
    match std::env::var_os(CONFIG_ENV) {
        Some(path) if !path.is_empty() => from_path(Path::new(&path)),
        Some(_) => invalid(),
        None => from_bytes(DEFAULT_ASSET),
    }
}

impl RootAxesBinding {
    /// Compile a strict primitive selection. Source existence and verbatim
    /// evidence remain the Sidecar's original source validation contract.
    pub(super) fn render_values(
        &self,
        stage: &str,
        axis_id: &str,
        selection: &Value,
        max_options: usize,
    ) -> Result<Value, String> {
        let eligible = self.eligible(stage)?;
        let configured = eligible
            .iter()
            .find(|axis| axis["id"] == axis_id)
            .ok_or_else(|| INVALID.to_owned())?;
        let selected: ValueSelection =
            serde_json::from_value(selection.clone()).map_err(|_| INVALID.to_owned())?;
        if !(2..=5).contains(&max_options)
            || !(2..=max_options).contains(&selected.options.len())
            || selected
                .options
                .iter()
                .filter(|item| item.recommended)
                .count()
                != 1
        {
            return invalid();
        }
        let capabilities = value_capabilities(VALUE_ASSET)?;
        let axis = capabilities
            .axes
            .iter()
            .find(|axis| axis.axis_id == axis_id)
            .ok_or_else(|| INVALID.to_owned())?;
        let allowed = configured["value_ids"]
            .as_array()
            .ok_or_else(|| INVALID.to_owned())?;
        let mut ids = HashSet::new();
        let mut options = Vec::with_capacity(selected.options.len());
        for item in selected.options {
            if !ids.insert(item.value_id.clone())
                || !allowed.iter().any(|id| id == &item.value_id)
                || !valid_memory_choice(&item.memory_choice)
            {
                return invalid();
            }
            let value = axis
                .values
                .iter()
                .find(|value| value.id == item.value_id)
                .ok_or_else(|| INVALID.to_owned())?;
            options.push(serde_json::json!({"value_id":value.id,"label":value.label,
                "description":value.description,"recommended":item.recommended,"memory_choice":item.memory_choice}));
        }
        Ok(
            serde_json::json!({"type":axis.question_type,"single_choice_reason":axis.single_choice_reason,
            "prompt_body":axis.prompt_body,"why_now":axis.why_now,"answer_template":axis.answer_template,"options":options}),
        )
    }
    fn checked_config(&self) -> Result<Config, String> {
        if self.config_sha256.len() != 64
            || !self
                .config_sha256
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return invalid();
        }
        let config: Config =
            serde_json::from_value(self.config.clone()).map_err(|_| INVALID.to_owned())?;
        validate_config(&config)?;
        Ok(config)
    }

    pub(super) fn eligible(&self, stage: &str) -> Result<Vec<Value>, String> {
        if !matches!(stage, "direction" | "detail") {
            return invalid();
        }
        self.checked_config()?
            .axes
            .into_iter()
            .filter(|axis| axis.stages.iter().any(|value| value == stage))
            .map(|axis| serde_json::to_value(axis).map_err(|_| INVALID.to_owned()))
            .collect()
    }

    /// An implemented property is one complete decision. Program-owned root
    /// questions resolve it directly; arbitrary child tasks are not refinements
    /// of this property and cannot be invented to fill a depth requirement.
    pub(super) fn validate_atomic_goals(
        &self,
        stage: &str,
        goals: &[String],
    ) -> Result<(), String> {
        let ids = self.validate_goals(stage, goals)?;
        let eligible = self.eligible(stage)?;
        if ids.iter().any(|id| {
            !eligible.iter().any(|axis| {
                axis["id"].as_str() == Some(id.as_str()) && axis["decision_mode"] == "atomic"
            })
        }) {
            return invalid();
        }
        Ok(())
    }

    /// Compile the complete ordered selection. The model cannot name a wider
    /// topic, repeat an attribute under another title, or invent a fact task.
    /// Balanced groups preserve all selected attributes under actual budgets.
    pub(super) fn compile_topics(
        &self,
        stage: &str,
        axis_ids: &[String],
        max_topics: usize,
        root_limit: usize,
    ) -> Result<Vec<Value>, String> {
        let eligible = self.eligible(stage)?;
        if max_topics == 0 || root_limit == 0 || axis_ids.is_empty() {
            return Err("主题属性选择或主题、根目标额度为空".into());
        }
        let mut seen = HashSet::new();
        let mut selected = Vec::with_capacity(axis_ids.len());
        for id in axis_ids {
            let axis = eligible
                .iter()
                .find(|axis| axis["id"].as_str() == Some(id.as_str()))
                .ok_or_else(|| "主题属性不存在或不适用于当前阶段".to_owned())?;
            if !seen.insert(id) {
                return Err("主题属性重复，必须保留互不重复的属性选择".into());
            }
            selected.push(axis);
        }
        let group_count = max_topics.min(selected.len());
        let base = selected.len() / group_count;
        let extra = selected.len() % group_count;
        let mut cursor = 0;
        let mut topics = Vec::with_capacity(group_count);
        for index in 0..group_count {
            let size = base + usize::from(index < extra);
            if size > root_limit {
                return Err("主题属性选择超出实际根目标额度，不能裁剪属性".into());
            }
            let axes = &selected[cursor..cursor + size];
            let title = axes
                .iter()
                .map(|axis| axis["title"].as_str().unwrap_or_default())
                .collect::<Vec<_>>()
                .join("与");
            let goal = axes
                .iter()
                .map(|axis| axis["goal"].as_str().unwrap_or_default())
                .collect::<Vec<_>>()
                .join("；");
            if title.chars().count() > 60 || goal.chars().count() > 240 {
                return Err("完整主题超过标题或目标长度额度，不能裁剪属性".into());
            }
            topics.push(serde_json::json!({
                "id": format!("topic_axis_{}", index + 1),
                "title": title,
                "goal": goal,
                "root_axis_ids": axis_ids[cursor..cursor + size],
            }));
            cursor += size;
        }
        Ok(topics)
    }

    /// Recompile the whole manifest, including grouping, order and prose. No
    /// unchecked field or wider topic can be persisted as program authority.
    pub(super) fn validate_topics(
        &self,
        stage: &str,
        topics: &[Value],
        max_topics: usize,
        root_limit: usize,
    ) -> Result<(), String> {
        let mut axis_ids = Vec::new();
        for topic in topics {
            let ids = topic["root_axis_ids"]
                .as_array()
                .ok_or_else(|| "主题缺少可信属性绑定".to_owned())?;
            if ids.is_empty() {
                return Err("主题属性绑定为空".into());
            }
            for id in ids {
                axis_ids.push(
                    id.as_str()
                        .ok_or_else(|| "主题属性绑定类型无效".to_owned())?
                        .to_owned(),
                );
            }
        }
        if self.compile_topics(stage, &axis_ids, max_topics, root_limit)? != topics {
            return Err("主题清单与完整属性编译结果不一致".into());
        }
        Ok(())
    }

    /// Match complete program goals in order. Returned values are canonical
    /// IDs; unknown prose, repetition and aliases reject the complete result.
    pub(super) fn validate_goals(
        &self,
        stage: &str,
        goals: &[String],
    ) -> Result<Vec<String>, String> {
        let eligible = self.eligible(stage)?;
        if goals.is_empty() || goals.len() > eligible.len() {
            return invalid();
        }
        let mut seen = HashSet::new();
        goals
            .iter()
            .map(|goal| {
                let axis = eligible
                    .iter()
                    .find(|axis| axis["goal"].as_str() == Some(goal.as_str()))
                    .ok_or_else(|| INVALID.to_owned())?;
                let id = axis["id"].as_str().ok_or_else(|| INVALID.to_owned())?;
                if !seen.insert(id) {
                    return invalid();
                }
                Ok(id.to_owned())
            })
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn default_binding() -> RootAxesBinding {
        from_bytes(DEFAULT_ASSET).unwrap()
    }
    fn parse(value: &Value) -> Result<RootAxesBinding, String> {
        from_bytes(&serde_json::to_vec(value).unwrap())
    }
    fn goal(binding: &RootAxesBinding, id: &str) -> String {
        binding.config["axes"]
            .as_array()
            .unwrap()
            .iter()
            .find(|axis| axis["id"] == id)
            .unwrap()["goal"]
            .as_str()
            .unwrap()
            .into()
    }

    #[test]
    fn shared_default_exact_hash_and_stage_scope() {
        let binding = default_binding();
        assert_eq!(
            binding.config_sha256,
            format!("{:x}", Sha256::digest(DEFAULT_ASSET))
        );
        assert_eq!(binding.eligible("detail").unwrap().len(), 11);
        let direction = binding.eligible("direction").unwrap();
        assert_eq!(
            direction
                .iter()
                .map(|a| a["id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec![
                "audience",
                "purpose",
                "content_focus",
                "narrative_perspective",
                "tone"
            ]
        );
        assert!(binding.eligible("writing").is_err());
    }

    #[test]
    fn two_distinct_attributes_keep_original_order_and_whole_goals() {
        let binding = default_binding();
        let goals = vec![
            goal(&binding, "information_granularity"),
            goal(&binding, "audience"),
        ];
        assert_eq!(
            binding.validate_goals("detail", &goals).unwrap(),
            vec!["information_granularity", "audience"]
        );
        assert!(binding.validate_goals("direction", &goals).is_err());
        assert_eq!(goals[0], goal(&binding, "information_granularity"));
    }

    #[test]
    fn unknown_alias_duplicate_and_empty_goals_reject() {
        let binding = default_binding();
        let audience = goal(&binding, "audience");
        for goals in [
            vec![],
            vec!["选择受众范围".into()],
            vec![audience.clone(), audience.clone()],
            vec![format!("{} ", audience)],
        ] {
            assert!(binding.validate_goals("direction", &goals).is_err());
        }
    }

    #[test]
    fn configurable_subset_order_and_stage_restriction_preserve_exact_mapping() {
        let mut value = default_binding().config;
        let mut axes = value["axes"].as_array().unwrap().clone();
        axes.retain(|axis| axis["id"] == "purpose" || axis["id"] == "audience");
        axes.reverse();
        axes[0]["stages"] = json!(["detail"]);
        value["axes"] = json!(axes);
        value["registry_version"] = json!(5);
        let binding = parse(&value).unwrap();
        assert_eq!(
            binding
                .eligible("detail")
                .unwrap()
                .iter()
                .map(|a| a["id"].as_str().unwrap())
                .collect::<Vec<_>>(),
            vec!["purpose", "audience"]
        );
        assert_eq!(binding.eligible("direction").unwrap().len(), 1);
        assert_ne!(binding.config_sha256, default_binding().config_sha256);
    }

    #[test]
    fn config_cannot_define_alias_property_new_prose_or_extra_fields() {
        let binding = default_binding();
        for (field, changed) in [
            ("id", json!("audience_range")),
            ("property", json!("audience_range")),
            ("title", json!("自由主题")),
            ("decision_mode", json!("expandable")),
            ("goal", json!("只询问主要受众")),
            ("definition", json!("把详略改成受众别名")),
            ("stages", json!(["writing"])),
            ("extra", json!(true)),
        ] {
            let mut value = binding.config.clone();
            value["axes"][0][field] = changed;
            assert!(parse(&value).is_err(), "{}", field);
        }
        let mut value = binding.config.clone();
        value["extra"] = json!("not authority");
        assert!(parse(&value).is_err());
    }

    #[test]
    fn duplicate_axes_stages_and_unsupported_direction_capability_reject() {
        let binding = default_binding();
        let mut duplicate = binding.config.clone();
        duplicate["axes"][1] = duplicate["axes"][0].clone();
        assert!(parse(&duplicate).is_err());
        let mut stage = binding.config.clone();
        stage["axes"][0]["stages"] = json!(["direction", "direction"]);
        assert!(parse(&stage).is_err());
        stage["axes"][3]["stages"] = json!(["direction"]);
        assert!(parse(&stage).is_err());
    }

    #[test]
    fn original_bytes_duplicate_fields_and_invalid_scalar_types_reject() {
        let text = std::str::from_utf8(DEFAULT_ASSET).unwrap();
        let duplicated = text.replacen(
            "\"schema_version\": 1",
            "\"schema_version\": 1, \"schema_version\": 1",
            1,
        );
        assert!(from_bytes(duplicated.as_bytes()).is_err());
        for value in [json!(true), json!(0), json!("1")] {
            let mut config = default_binding().config;
            config["registry_version"] = value;
            assert!(parse(&config).is_err());
        }
        assert!(from_bytes(b"{}").is_err());
        assert!(from_bytes(&vec![b' '; MAX_REGISTRY_BYTES + 1]).is_err());
    }

    #[test]
    fn explicit_file_loading_freezes_hash_from_actual_bytes() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("root-axes-v1.json");
        std::fs::write(&path, DEFAULT_ASSET).unwrap();
        let first = from_path(&path).unwrap();
        std::fs::write(&path, [DEFAULT_ASSET, b"\n"].concat()).unwrap();
        let second = from_path(&path).unwrap();
        assert_eq!(first.config, second.config);
        assert_ne!(first.config_sha256, second.config_sha256);
        assert!(from_path(&directory.path().join("missing")).is_err());
    }

    #[test]
    fn forged_binding_shape_or_hash_syntax_cannot_compile() {
        let mut binding = default_binding();
        binding.config_sha256 = "g".repeat(64);
        assert!(binding.eligible("direction").is_err());
        assert!(serde_json::from_value::<RootAxesBinding>(
            json!({"config_sha256":"0".repeat(64), "config":binding.config, "extra":true})
        )
        .is_err());
    }

    fn audience_selection() -> Value {
        json!({"options":[
            {"value_id":"audience.participants","recommended":false,"memory_choice":"reference:m1+m2"},
            {"value_id":"audience.decision_makers","recommended":true,"memory_choice":"original"}
        ]})
    }

    #[test]
    fn installed_values_close_both_hash_and_selector_authority() {
        let binding = default_binding();
        assert_eq!(
            binding.config["value_capabilities_sha256"],
            format!("{:x}", Sha256::digest(VALUE_ASSET))
        );
        for changed in [json!("0".repeat(64)), json!(null)] {
            let mut config = binding.config.clone();
            config["value_capabilities_sha256"] = changed;
            assert!(parse(&config).is_err());
        }
        for selected in [
            json!([]),
            json!(["audience.participants"]),
            json!(["audience.participants", "purpose.understand_state"]),
            json!(["audience.participants", "audience.participants"]),
            json!(["audience.decision_makers", "audience.participants"]),
        ] {
            let mut config = binding.config.clone();
            config["axes"][0]["value_ids"] = selected;
            assert!(parse(&config).is_err());
        }
        let mut config = binding.config.clone();
        config["axes"][0]["value_ids"] = json!(["audience.participants", "audience.collaborators"]);
        let restricted = parse(&config).unwrap();
        assert!(restricted
            .render_values("direction", "audience", &audience_selection(), 5)
            .is_err());
        config["axes"][0]["values"] = json!([{"label":"受众加ROI与指标优先"}]);
        assert!(parse(&config).is_err());
    }

    #[test]
    fn finite_values_render_exact_installed_copy_without_invented_strategy_or_progress() {
        let rendered = default_binding()
            .render_values("direction", "audience", &audience_selection(), 5)
            .unwrap();
        assert_eq!(rendered["prompt_body"], "本次创作面向哪些阅读对象？");
        assert_eq!(rendered["why_now"], "确定阅读对象范围，供后续创作沿用。");
        assert_eq!(rendered["type"], "multi_choice");
        assert_eq!(rendered["single_choice_reason"], "");
        assert_eq!(
            rendered["options"][0],
            json!({"value_id":"audience.participants",
            "label":"执行参与者","description":"承担相关执行工作、直接参与事情推进的读者。",
            "recommended":false,"memory_choice":"reference:m1+m2"})
        );
        assert_eq!(rendered["options"][1]["label"], "管理决策者");
        assert_eq!(rendered["options"][1]["recommended"], true);
        assert!(rendered.get("next_goals").is_none());
    }

    #[test]
    fn free_copy_and_mixed_protocol_cannot_enter_the_finite_renderer() {
        let binding = default_binding();
        for field in [
            "question",
            "type",
            "next_goals",
            "why_now",
            "single_choice_reason",
            "root_value_selection",
        ] {
            let mut raw = audience_selection();
            raw[field] = json!("周报已生成分版");
            assert!(
                binding
                    .render_values("direction", "audience", &raw, 5)
                    .is_err(),
                "{}",
                field
            );
        }
        for field in [
            "label",
            "description",
            "next_goals",
            "memory_ids",
            "memory_evidence",
            "id",
        ] {
            let mut raw = audience_selection();
            raw["options"][0][field] = json!("侧重硬件利用率、ROI和信息粒度");
            assert!(
                binding
                    .render_values("direction", "audience", &raw, 5)
                    .is_err(),
                "{}",
                field
            );
        }
        for value in [
            json!("audience.all"),
            json!("purpose.understand_state"),
            json!("audience.participants "),
            json!(true),
        ] {
            let mut raw = audience_selection();
            raw["options"][0]["value_id"] = value;
            assert!(binding
                .render_values("direction", "audience", &raw, 5)
                .is_err());
        }
        let mut raw = audience_selection();
        raw["options"][1]["value_id"] = raw["options"][0]["value_id"].clone();
        assert!(binding
            .render_values("direction", "audience", &raw, 5)
            .is_err());
    }

    #[test]
    fn selection_count_recommendation_types_memory_syntax_and_stage_are_strict() {
        let binding = default_binding();
        for raw in [
            json!({"options":[]}),
            json!({"options":[audience_selection()["options"][0]]}),
            json!(null),
        ] {
            assert!(binding
                .render_values("direction", "audience", &raw, 5)
                .is_err());
        }
        for recommendation in [json!(true), json!(1), json!("false"), json!(null)] {
            let mut raw = audience_selection();
            raw["options"][0]["recommended"] = recommendation;
            assert!(binding
                .render_values("direction", "audience", &raw, 5)
                .is_err());
        }
        let mut raw = audience_selection();
        raw["options"][1]["recommended"] = json!(false);
        assert!(binding
            .render_values("direction", "audience", &raw, 5)
            .is_err());
        for choice in [
            "",
            "original ",
            "reference:",
            "reference:m0",
            "reference:m1+m1",
            "reference:m1+m2+m3",
            "reference:invented",
        ] {
            let mut raw = audience_selection();
            raw["options"][0]["memory_choice"] = json!(choice);
            assert!(
                binding
                    .render_values("direction", "audience", &raw, 5)
                    .is_err(),
                "{}",
                choice
            );
        }
        assert!(binding
            .render_values("writing", "audience", &audience_selection(), 5)
            .is_err());
        assert!(binding
            .render_values(
                "direction",
                "information_granularity",
                &audience_selection(),
                5
            )
            .is_err());
        assert!(binding
            .render_values("direction", "audience", &audience_selection(), 1)
            .is_err());
        assert!(binding
            .render_values("direction", "audience", &audience_selection(), 6)
            .is_err());
    }

    #[test]
    fn all_installed_attribute_values_are_available_without_model_copy_fields() {
        let binding = default_binding();
        let asset: Value = serde_json::from_slice(VALUE_ASSET).unwrap();
        for axis in asset["axes"].as_array().unwrap() {
            let options: Vec<Value> = axis["values"].as_array().unwrap().iter().enumerate().map(|(i,v)|
                json!({"value_id":v["id"],"recommended":i==0,"memory_choice":"original"})).collect();
            let selection = json!({"options":options});
            let rendered = binding
                .render_values("detail", axis["axis_id"].as_str().unwrap(), &selection, 5)
                .unwrap();
            for key in [
                "type",
                "prompt_body",
                "why_now",
                "single_choice_reason",
                "answer_template",
            ] {
                assert_eq!(rendered[key], axis[key]);
            }
            assert_eq!(
                rendered["options"].as_array().unwrap().len(),
                axis["values"].as_array().unwrap().len()
            );
        }
    }

    fn ids(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    #[test]
    fn direction_topics_are_exact_independent_attributes_not_wider_free_prose() {
        let binding = default_binding();
        let selected = ids(&[
            "audience",
            "purpose",
            "content_focus",
            "narrative_perspective",
            "tone",
        ]);
        let topics = binding
            .compile_topics("direction", &selected, 6, 4)
            .unwrap();
        assert_eq!(topics.len(), 5);
        assert_eq!(
            topics[0],
            json!({
                "id":"topic_axis_1", "title":"阅读对象范围",
                "goal":goal(&binding, "audience"), "root_axis_ids":["audience"]
            })
        );
        binding.validate_topics("direction", &topics, 6, 4).unwrap();
        let mut wider = topics.clone();
        wider[0]["goal"] = json!("确定汇报对象以及表达目的、侧重和视角");
        assert!(binding.validate_topics("direction", &wider, 6, 4).is_err());
        wider = topics.clone();
        wider[0]["root_axis_ids"] = json!([
            "audience",
            "purpose",
            "content_focus",
            "narrative_perspective"
        ]);
        assert!(binding.validate_topics("direction", &wider, 6, 4).is_err());
    }

    #[test]
    fn detail_balanced_groups_preserve_every_ordered_attribute_and_multiple_roots() {
        let binding = default_binding();
        let selected: Vec<String> = binding
            .eligible("detail")
            .unwrap()
            .iter()
            .map(|axis| axis["id"].as_str().unwrap().to_owned())
            .rev()
            .collect();
        let topics = binding.compile_topics("detail", &selected, 6, 2).unwrap();
        assert_eq!(topics.len(), 6);
        assert_eq!(
            topics
                .iter()
                .map(|topic| topic["root_axis_ids"].as_array().unwrap().len())
                .collect::<Vec<_>>(),
            vec![2, 2, 2, 2, 2, 1]
        );
        let flat: Vec<String> = topics
            .iter()
            .flat_map(|topic| topic["root_axis_ids"].as_array().unwrap())
            .map(|id| id.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(flat, selected);
        assert_eq!(topics[0]["title"], "行动实施方式与表达语气");
        assert_eq!(
            topics[0]["goal"],
            format!(
                "{}；{}",
                goal(&binding, "action_plan"),
                goal(&binding, "tone")
            )
        );
        binding.validate_topics("detail", &topics, 6, 2).unwrap();
    }

    #[test]
    fn whole_selection_rejects_duplicates_unknown_ineligible_and_bad_budgets() {
        let binding = default_binding();
        for selection in [
            vec![],
            ids(&["audience", "audience"]),
            ids(&["audience", "statistics_period"]),
            ids(&["information_granularity"]),
            ids(&["audience "]),
        ] {
            assert!(binding
                .compile_topics("direction", &selection, 6, 4)
                .is_err());
        }
        let selected = ids(&[
            "audience",
            "purpose",
            "content_focus",
            "narrative_perspective",
            "tone",
        ]);
        assert!(binding
            .compile_topics("direction", &selected, 0, 4)
            .is_err());
        assert!(binding
            .compile_topics("direction", &selected, 6, 0)
            .is_err());
        assert!(binding
            .compile_topics("direction", &selected, 2, 2)
            .is_err());
        let grouped = binding
            .compile_topics("direction", &selected, 2, 3)
            .unwrap();
        assert_eq!(
            grouped[0]["root_axis_ids"],
            json!(["audience", "purpose", "content_focus"])
        );
        assert_eq!(
            grouped[1]["root_axis_ids"],
            json!(["narrative_perspective", "tone"])
        );
        let all = binding
            .eligible("detail")
            .unwrap()
            .iter()
            .map(|axis| axis["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert!(binding
            .compile_topics("detail", &all, 1, all.len())
            .is_err());
        assert_eq!(selected.len(), 5);
        assert_eq!(all.len(), 11);
    }

    #[test]
    fn whole_manifest_rejects_regrouping_reordering_metadata_and_omitted_binding() {
        let binding = default_binding();
        let topics = binding
            .compile_topics("direction", &ids(&["audience", "purpose"]), 6, 4)
            .unwrap();
        for (field, value) in [
            ("id", json!("topic_axis_2")),
            ("title", json!("确定统计口径")),
            ("goal", json!("选择当前主题统计周期")),
            ("chapter_id", json!("chapter_1")),
            ("root_axis_ids", json!([])),
            ("root_axis_ids", json!([true])),
            ("root_axis_ids", json!(["purpose"])),
        ] {
            let mut changed = topics.clone();
            changed[0][field] = value;
            assert!(
                binding
                    .validate_topics("direction", &changed, 6, 4)
                    .is_err(),
                "{}",
                field
            );
        }
        let mut omitted = topics.clone();
        omitted[0].as_object_mut().unwrap().remove("root_axis_ids");
        assert!(binding
            .validate_topics("direction", &omitted, 6, 4)
            .is_err());
        let mut reversed = topics.clone();
        reversed.reverse();
        assert!(binding
            .validate_topics("direction", &reversed, 6, 4)
            .is_err());
        let one_group = binding
            .compile_topics("direction", &ids(&["audience", "purpose"]), 1, 4)
            .unwrap();
        assert!(binding
            .validate_topics("direction", &one_group, 6, 4)
            .is_err());
    }

    #[test]
    fn only_exact_atomic_properties_authorize_terminal_questions() {
        let binding = default_binding();
        for stage in ["direction", "detail"] {
            let axes = binding.eligible(stage).unwrap();
            assert!(axes.iter().all(|axis| axis["decision_mode"] == "atomic"));
            let goals = axes
                .iter()
                .map(|axis| axis["goal"].as_str().unwrap().to_owned())
                .collect::<Vec<_>>();
            binding.validate_atomic_goals(stage, &goals).unwrap();
        }
        for goals in [
            vec![],
            vec!["确认技术团队关注的具体指标维度".into()],
            vec!["确认是否需要展示硬件缩容后的成本节省数据".into()],
            vec![goal(&binding, "audience"), goal(&binding, "audience")],
        ] {
            assert!(binding.validate_atomic_goals("direction", &goals).is_err());
        }
        let mut forged = binding.clone();
        forged.config["axes"][0]["decision_mode"] = json!(true);
        assert!(forged
            .validate_atomic_goals("direction", &[goal(&binding, "audience")])
            .is_err());
    }
}
