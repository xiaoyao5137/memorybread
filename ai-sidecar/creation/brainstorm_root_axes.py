"""Compile configured, implemented creative attributes into root goals.

The registry selects capabilities, not user answers or arbitrary new goal
prose. Different registered attributes do not prove that free question copy
actually follows them; the existing semantic and content gates still apply.
"""

import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

from .brainstorm import BrainstormGenerationError
from .brainstorm_memory_policy import bounded_memory_choices


MAX_REGISTRY_BYTES = 128 * 1024
REGISTRY_ID = "generic-creation-attributes"
VALUE_CAPABILITIES_SHA256 = "0e89c1dbd4f160d4a645523d412dfa723a97d2e48fd8525800740ba1c0d786be"
VALUE_RENDERER_ID = "atomic_values_v1"
# Canonical records are implemented capabilities. Configuration may only
# select/order them and restrict their supported stages.
_AXES = (
    ("audience", "作品面向哪些阅读对象；汇报对象、阅读对象和受众范围均属于这一属性，不预设单一对象，不包含信息粒度或概念解释深度。",
     "在全部既定要求内，选择当前主题尚未确定的阅读对象范围"),
    ("purpose", "作品希望读者理解、判断或采取什么行动；这是表达目的，不是内容侧重、语气或行动的具体实施方法。",
     "在全部既定要求内，确定当前主题尚未确定的表达目的"),
    ("content_focus", "保留全部必需内容时，哪些内容在表达中获得更突出的位置与说明；不选择删去必需内容，也不改变表达目的。",
     "保留全部必需内容，确定当前主题尚未确定的表达侧重"),
    ("information_granularity", "完整必需信息在表达中的呈现粒度；不改变必需字段、数据关系、计算规则或概念解释的深度。",
     "保留全部必需信息及其关系，确定当前主题尚未确定的信息呈现粒度"),
    ("concept_explanation_depth", "对概念、术语与原理补充多少解释；不重新选择读者对象，不改变数据呈现粒度或删去必需信息。",
     "保留全部必需内容，确定当前主题尚未确定的概念解释深度"),
    ("evidence_presentation", "已有完整论据怎样支持表达中的判断；不选择资料权限、替换来源、删去必需论据或制造事实。",
     "保留全部必需论据与来源，确定当前主题尚未确定的论据呈现方式"),
    ("narrative_perspective", "叙述从哪个观察位置展开；不重新选择读者、表达目的或事件推进节奏。",
     "在全部既定要求内，确定当前主题尚未确定的叙事视角"),
    ("structural_order", "尚未安排的表达单元采用什么组织次序；保留既定顺序与完整内容，不改变事件推进节奏或重排固定数据关系。",
     "保留全部既定内容与安排，确定当前主题新增表达单元的组织次序"),
    ("rhythm", "当前表达怎样分配展开与收束的节奏；不改变既定内容、表达单元的组织次序或叙事视角。",
     "保留全部既定内容，确定当前主题尚未确定的展开节奏"),
    ("tone", "表达采用怎样的语气；不重新选择受众、目的、事实结论或必需内容。",
     "在全部既定要求内，确定当前主题尚未确定的表达语气"),
    ("action_plan", "在全部既定目标与要求内，行动采用什么具体实施方式；不更改目标、取消固定动作、补造资源或制造成果。",
     "保留全部既定目标与要求，确定当前主题尚未确定的行动实施方式"),
)
_TITLES = (
    "阅读对象范围", "表达目的", "表达侧重", "信息呈现粒度", "概念解释深度", "论据呈现",
    "叙事视角", "新增表达单元的组织次序", "展开节奏", "表达语气", "行动实施方式",
)
IMPLEMENTED_AXES: Dict[str, Dict[str, Any]] = {
    identifier: {"id": identifier, "property": identifier, "definition": definition,
        "goal": goal, "title": title, "decision_mode": "atomic", "stages": (["direction", "detail"] if identifier in {
            "audience", "purpose", "content_focus", "narrative_perspective", "tone"} else ["detail"])}
    for (identifier, definition, goal), title in zip(_AXES, _TITLES)
}


def _fail() -> None:
    raise BrainstormGenerationError("可信根属性目录无效、已变更或与当前配置绑定不一致")


def _registry_path() -> Path:
    override = os.environ.get("MEMORYBREAD_BRAINSTORM_ROOT_AXES_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "creation_brainstorm" / "root-axes-v1.json"
    return Path(__file__).resolve().parents[2] / "shared" / "creation-brainstorm" / "root-axes-v1.json"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _exact_json(first: Any, second: Any) -> bool:
    # Python deep equality treats True as 1. Bind the actual JSON primitive
    # types as well as their values, without normalizing a request snapshot.
    try:
        return json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) == json.dumps(
            second, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return False


def _value_capabilities_path() -> Path:
    # Registry configuration can select implemented values, but cannot point
    # at another file or replace the renderer's reviewed wording.
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "creation_brainstorm" / "attribute-values-v1.json"
    return Path(__file__).resolve().parents[2] / "shared" / "creation-brainstorm" / "attribute-values-v1.json"


def load_attribute_values() -> Dict[str, Any]:
    """Load one implemented renderer; actual bytes are part of its identity."""
    try:
        with _value_capabilities_path().open("rb") as source:
            raw = source.read(MAX_REGISTRY_BYTES + 1)
        if not 0 < len(raw) <= MAX_REGISTRY_BYTES or hashlib.sha256(raw).hexdigest() != VALUE_CAPABILITIES_SHA256:
            _fail()
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {
                "schema_version", "kind", "capability_version", "renderer_id", "axes"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["kind"] != "memorybread.brainstorm-attribute-values"
                or type(value["capability_version"]) is not int or value["capability_version"] != 1
                or value["renderer_id"] != VALUE_RENDERER_ID
                or not isinstance(value["axes"], list) or len(value["axes"]) != len(IMPLEMENTED_AXES)):
            _fail()
        seen = set()
        for axis in value["axes"]:
            if (not isinstance(axis, dict) or set(axis) != {"axis_id", "type", "single_choice_reason",
                    "prompt_body", "why_now", "answer_template", "values"}
                    or not isinstance(axis["axis_id"], str) or axis["axis_id"] not in IMPLEMENTED_AXES
                    or axis["axis_id"] in seen or axis["type"] not in {"single_choice", "multi_choice"}
                    or not isinstance(axis["values"], list) or not 2 <= len(axis["values"]) <= 5):
                _fail()
            seen.add(axis["axis_id"])
            for field, maximum in (("prompt_body", 60), ("why_now", 80), ("answer_template", 80),
                                   ("single_choice_reason", 80)):
                text = axis[field]
                if (not isinstance(text, str) or text != text.strip() or len(text) > maximum
                        or (field != "single_choice_reason" and not text)):
                    _fail()
            if bool(axis["single_choice_reason"]) != (axis["type"] == "single_choice"):
                _fail()
            ids, labels = set(), set()
            for item in axis["values"]:
                if (not isinstance(item, dict) or set(item) != {"id", "label", "description"}
                        or not isinstance(item["id"], str) or not re.fullmatch(
                            re.escape(axis["axis_id"]) + r"\.[a-z][a-z0-9_]*", item["id"])
                        or item["id"] in ids):
                    _fail()
                for field, maximum in (("label", 24), ("description", 80)):
                    if (not isinstance(item[field], str) or not item[field]
                            or item[field] != item[field].strip() or len(item[field]) > maximum):
                        _fail()
                key = re.sub(r"\s+", "", item["label"]).casefold()
                if key in labels:
                    _fail()
                labels.add(key)
                ids.add(item["id"])
        return value
    except BrainstormGenerationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        _fail()


def _validate_config(config: Any) -> Dict[str, Any]:
    capabilities = {axis["axis_id"]: axis for axis in load_attribute_values()["axes"]}
    if (not isinstance(config, dict) or set(config) != {
            "schema_version", "kind", "registry_id", "registry_version", "axes", "value_capabilities_sha256"}
            or type(config["schema_version"]) is not int or config["schema_version"] != 1
            or config["kind"] != "memorybread.brainstorm-root-axes"
            or config["registry_id"] != REGISTRY_ID
            or type(config["registry_version"]) is not int or config["registry_version"] < 4
            or config["value_capabilities_sha256"] != VALUE_CAPABILITIES_SHA256
            or not isinstance(config["axes"], list) or not 1 <= len(config["axes"]) <= len(IMPLEMENTED_AXES)):
        _fail()
    ids, properties = set(), set()
    for axis in config["axes"]:
        if (not isinstance(axis, dict) or set(axis) != {"id", "property", "definition", "goal", "title", "decision_mode", "stages", "value_ids"}
                or not isinstance(axis["id"], str) or axis["id"] not in IMPLEMENTED_AXES
                or not isinstance(axis["property"], str) or axis["id"] in ids
                or axis["property"] in properties):
            _fail()
        canonical = IMPLEMENTED_AXES[axis["id"]]
        value_ids = [item["id"] for item in capabilities[axis["id"]]["values"]]
        if (any(axis[field] != canonical[field] for field in ("id", "property", "definition", "goal", "title", "decision_mode"))
                or not isinstance(axis["goal"], str) or not 1 <= len(axis["goal"]) <= 80
                or not isinstance(axis["title"], str) or not 1 <= len(axis["title"]) <= 60
                or not isinstance(axis["stages"], list) or not axis["stages"]
                or any(not isinstance(stage, str) or stage not in canonical["stages"] for stage in axis["stages"])
                or len(set(axis["stages"])) != len(axis["stages"])):
            _fail()
        if (not isinstance(axis["value_ids"], list) or not 2 <= len(axis["value_ids"]) <= len(value_ids)
                or any(not isinstance(identifier, str) for identifier in axis["value_ids"])
                or axis["value_ids"] != [identifier for identifier in value_ids if identifier in axis["value_ids"]]):
            _fail()
        ids.add(axis["id"])
        properties.add(axis["property"])
    return copy.deepcopy(config)


def load_root_axes_binding() -> Dict[str, Any]:
    """Read actual installed asset bytes; a request blob is never authority."""
    try:
        with _registry_path().open("rb") as source:
            raw = source.read(MAX_REGISTRY_BYTES + 1)
        if not 0 < len(raw) <= MAX_REGISTRY_BYTES:
            _fail()
        config = _validate_config(json.loads(raw, object_pairs_hook=_unique_object))
        return {"config_sha256": hashlib.sha256(raw).hexdigest(), "config": config}
    except BrainstormGenerationError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        _fail()


class RootAxisCompiler:
    """Verified local capabilities; never repairs or drops invalid selections."""

    def __init__(self, binding: Any):
        if (not isinstance(binding, dict) or set(binding) != {"config_sha256", "config"}
                or not isinstance(binding["config_sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", binding["config_sha256"])):
            _fail()
        actual = load_root_axes_binding()
        if not _exact_json(binding, actual):
            _fail()
        self.binding = copy.deepcopy(actual)

    def eligible_axes(self, stage: str) -> List[Dict[str, Any]]:
        self.verify_binding()
        if not isinstance(stage, str) or stage not in {"direction", "detail"}:
            _fail()
        return [copy.deepcopy(axis) for axis in self.binding["config"]["axes"] if stage in axis["stages"]]

    def verify_binding(self) -> None:
        if not _exact_json(load_root_axes_binding(), self.binding):
            _fail()

    def value_capability(self, axis: Any) -> Dict[str, Any]:
        """Return the immutable wording selected by this exact configured axis."""
        self.verify_binding()
        if not isinstance(axis, dict) or not any(_exact_json(axis, item) for item in self.binding["config"]["axes"]):
            raise BrainstormGenerationError("属性值能力未绑定当前安装目录内的完整属性")
        capability = next(item for item in load_attribute_values()["axes"] if item["axis_id"] == axis["id"])
        result = copy.deepcopy(capability)
        result["values"] = [item for item in result["values"] if item["id"] in axis["value_ids"]]
        return result

    def compile_values(self, raw: Any, axis: Dict[str, Any], max_options: int,
                       memory_ids: List[str]) -> Dict[str, Any]:
        """Render strict primitive selections without accepting model prose."""
        capability = self.value_capability(axis)
        if not isinstance(raw, dict) or set(raw) != {"options"}:
            raise BrainstormGenerationError("原子属性模型仅输出options属性值选择，不输出题干、说明、题型或后续目标")
        if (type(max_options) is not int or not 2 <= max_options <= 5
                or not isinstance(raw["options"], list) or not 2 <= len(raw["options"]) <= max_options):
            raise BrainstormGenerationError("原子属性值数量必须为2至max_options={}，不能删项或截断".format(max_options))
        values = {item["id"]: item for item in capability["values"]}
        choices = bounded_memory_choices(memory_ids)
        selected, seen, recommended_count = [], set(), 0
        for index, item in enumerate(raw["options"], start=1):
            if not isinstance(item, dict) or set(item) != {"value_id", "recommended", "memory_choice"}:
                raise BrainstormGenerationError("option_index={}仅可包含value_id、recommended、memory_choice".format(index))
            identifier = item["value_id"]
            if not isinstance(identifier, str) or identifier not in values:
                raise BrainstormGenerationError("option_index={}不是当前属性value_ids中的完整ID".format(index))
            if identifier in seen:
                raise BrainstormGenerationError("option_index={}重复属性值ID={}，不能重命名或删除".format(index, identifier))
            if type(item["recommended"]) is not bool:
                raise BrainstormGenerationError("option_index={}的recommended必须为布尔值".format(index))
            if not isinstance(item["memory_choice"], str) or item["memory_choice"] not in choices:
                raise BrainstormGenerationError("option_index={}的memory_choice不是本轮真实来源的有限选择".format(index))
            recommended_count += int(item["recommended"])
            seen.add(identifier)
            selected.append({"label": values[identifier]["label"], "description": values[identifier]["description"],
                "recommended": item["recommended"], "memory_choice": item["memory_choice"]})
        if recommended_count != 1:
            raise BrainstormGenerationError("原子属性值必须恰有1个recommended=true，实际={}，不能程序补选".format(recommended_count))
        question = {key: capability[key] for key in (
            "type", "single_choice_reason", "prompt_body", "why_now", "answer_template")}
        question["options"] = selected
        return {"question": question, "next_goals": []}

    def _selection(self, identifiers: Any, stage: str) -> List[Dict[str, Any]]:
        if not isinstance(identifiers, list) or not identifiers:
            raise BrainstormGenerationError("创作属性选择必须是非空有序ID数组")
        by_id = {axis["id"]: axis for axis in self.eligible_axes(stage)}
        selected, ids, properties = [], set(), set()
        for index, identifier in enumerate(identifiers, start=1):
            if not isinstance(identifier, str) or identifier not in by_id:
                raise BrainstormGenerationError("axis_index={}不是当前阶段目录内的属性ID；只能选择eligible_root_axes中的完整ID".format(index))
            if identifier in ids:
                raise BrainstormGenerationError("axis_index={}重复属性ID={}；每个属性在本阶段只能出现一次".format(index, identifier))
            axis = by_id[identifier]
            if axis["property"] in properties:
                raise BrainstormGenerationError("axis_index={}重复已选择的创作属性".format(index))
            ids.add(identifier)
            properties.add(axis["property"])
            selected.append(axis)
        return selected

    @staticmethod
    def _plan_budget(limits: Any) -> tuple:
        if (not isinstance(limits, dict) or any(type(limits.get(key)) is not int or limits[key] < 1
                for key in ("max_topics", "max_questions_per_topic", "max_depth"))
                or limits["max_topics"] > 12 or limits["max_questions_per_topic"] > 24
                or limits["max_depth"] > 3):
            raise BrainstormGenerationError("创作属性主题预算无效")
        # Every implemented property is one atomic decision. A hypothetical
        # option does not require unrelated invented child decisions.
        root_limit = limits["max_questions_per_topic"]
        if root_limit < 1:
            raise BrainstormGenerationError("创作属性主题预算不能完整展开一个根目标")
        return limits["max_topics"], root_limit

    def root_limit(self, limits: Dict[str, int]) -> int:
        self.verify_binding()
        return self._plan_budget(limits)[1]

    def compile_plan(self, raw: Any, stage: str, limits: Dict[str, int],
                     brief_edits: Any = None) -> Dict[str, Any]:
        """Choose attributes once; compile every selected item without slicing."""
        self.verify_binding()
        if not isinstance(raw, dict) or set(raw) != {"axis_ids"}:
            raise BrainstormGenerationError("自由阶段计划仅输出axis_ids，不输出主题文案、待决定事项或配置身份")
        axes = self._selection(raw["axis_ids"], stage)
        max_topics, root_limit = self._plan_budget(limits)
        count = min(max_topics, len(axes))
        base, extra = divmod(len(axes), count)
        topics, offset = [], 0
        for index in range(count):
            size = base + (1 if index < extra else 0)
            group = axes[offset:offset + size]
            offset += size
            if size > root_limit:
                raise BrainstormGenerationError("topic_index={}包含{}个属性，超过可完整展开的root_limit={}；selectedAxisCount={}，max_topics={}，不得删项或截断".format(
                    index + 1, size, root_limit, len(axes), max_topics))
            title = "与".join(axis["title"] for axis in group)
            goal = "；".join(axis["goal"] for axis in group)
            if len(title) > 60 or len(goal) > 240:
                raise BrainstormGenerationError("topic_index={}完整编译文案超出长度预算（titleChars={}，maxTitle=60，goalChars={}，maxGoal=240），不得截断".format(
                    index + 1, len(title), len(goal)))
            topics.append({"id": "topic_axis_" + str(index + 1), "title": title, "goal": goal,
                "root_axis_ids": [axis["id"] for axis in group]})
        edits = {} if brief_edits is None else brief_edits
        if not isinstance(edits, dict) or not isinstance(edits.get("open_flags", ""), str):
            raise BrainstormGenerationError("用户待决定事项必须保留原始文字")
        flags = [line for line in edits.get("open_flags", "").splitlines() if line.strip()]
        if len(flags) > 12 or any(len(line) > 240 for line in flags):
            raise BrainstormGenerationError("用户待决定事项超出原有完整行预算，不能截断或改写")
        return {"topics": topics, "chapters": [], "open_flags": flags}

    def verify_plan(self, result: Any, raw: Any, stage: str, limits: Dict[str, int],
                    brief_edits: Any = None) -> None:
        if result != self.compile_plan(raw, stage, limits, brief_edits):
            raise BrainstormGenerationError("阶段计划与完整有序属性选择的程序编译结果不一致")

    def topic_goals(self, topic: Any, stage: str, limits: Dict[str, int],
                    root_limit: int, topics: Any) -> Dict[str, Any]:
        """Recompile the whole saved plan before binding this topic's roots."""
        if not isinstance(topics, list) or not topics or any(not isinstance(item, dict)
                or set(item) != {"id", "title", "goal", "root_axis_ids"}
                or not isinstance(item["root_axis_ids"], list) for item in topics):
            raise BrainstormGenerationError("自由主题根目标缺少完整的程序编译主题目录")
        identifiers = [identifier for item in topics for identifier in item["root_axis_ids"]]
        expected = self.compile_plan({"axis_ids": identifiers}, stage, limits)["topics"]
        if topics != expected or not isinstance(topic, dict) or topic not in expected:
            raise BrainstormGenerationError("主题或属性顺序与当前完整编译计划不一致，不能重选其它主题属性")
        return self.compile({"root_axis_ids": topic["root_axis_ids"]}, stage, root_limit)

    def verify_topic_goals(self, result: Any, topic: Any, stage: str, limits: Dict[str, int],
                           root_limit: int, topics: Any) -> None:
        if result != self.topic_goals(topic, stage, limits, root_limit, topics):
            raise BrainstormGenerationError("主题根目标与当前主题的完整绑定属性不一致")

    def atomic_axis(self, node: Any, stage: str, limits: Dict[str, int],
                    topic: Any, topics: Any, node_mode: Any) -> Dict[str, Any]:
        """Prove terminal capability from the exact installed registry/plan.

        The request mode only names the expected node and axis; it cannot
        grant termination or change a saved goal, parent or selected property.
        """
        roots = self.topic_goals(topic, stage, limits, self.root_limit(limits), topics)["root_goals"]
        if (not isinstance(node_mode, dict) or set(node_mode) != {"kind", "axis_id", "node_id"}
                or node_mode["kind"] != "root_axis_atomic"
                or not isinstance(node, dict) or set(node) != {"id", "goal", "depth", "max_options", "max_next_goals",
                    "parent_question_id", "parent_option_id", "ancestor_conditions"}
                or not isinstance(node["id"], str) or not 1 <= len(node["id"]) <= 80
                or node_mode["node_id"] != node["id"]
                or type(node["depth"]) is not int or node["depth"] != 1
                or type(node["max_options"]) is not int or not 2 <= node["max_options"] <= 5
                or type(node["max_next_goals"]) is not int or node["max_next_goals"] != 0
                or node["parent_question_id"] is not None or node["parent_option_id"] is not None
                or node["ancestor_conditions"] != []):
            raise BrainstormGenerationError("原子根节点模式必须精确绑定当前属性、实际根节点和空后续路径")
        # Core's execution ID ends with the immutable original root ordinal.
        # Never infer that ordinal from recommendation or rendered options.
        match = re.fullmatch(r"stage-.+-[1-9][0-9]*-(0|[1-9][0-9]*)", node["id"])
        if match is None:
            raise BrainstormGenerationError("原子根节点缺少有效的程序根序号")
        ordinal = int(match.group(1))
        if (ordinal >= len(roots) or node["goal"] != roots[ordinal]
                or node_mode["axis_id"] != topic["root_axis_ids"][ordinal]):
            raise BrainstormGenerationError("原子根节点的实际序号、完整目标与当前主题属性不一致")
        axis = next(axis for axis in self.eligible_axes(stage) if axis["id"] == node_mode["axis_id"])
        if axis["decision_mode"] != "atomic":
            raise BrainstormGenerationError("当前属性没有程序实现的原子决定能力")
        return copy.deepcopy(axis)

    def compile(self, raw: Any, stage: str, root_limit: int) -> Dict[str, Any]:
        self.verify_binding()
        if type(root_limit) is not int or root_limit < 1:
            _fail()
        if not isinstance(raw, dict) or set(raw) != {"root_axis_ids"}:
            raise BrainstormGenerationError("根目标绑定只能包含完整root_axis_ids")
        axes = self._selection(raw["root_axis_ids"], stage)
        if len(axes) > root_limit:
            raise BrainstormGenerationError("当前主题属性数={}超过root_limit={}，不能删除属性后发布".format(len(axes), root_limit))
        selected = [axis["goal"] for axis in axes]
        if len(set(selected)) != len(selected):
            _fail()
        return {"root_goals": selected}

    def verify(self, result: Any, raw: Any, stage: str, root_limit: int) -> None:
        if result != self.compile(raw, stage, root_limit):
            _fail()
