"""Finite writing choices, not a proof of arbitrary natural-language policies.

The compiler owns every displayed action and every child edge. Its interpreter
only creates a separate layer of proposed editorial additions; it cannot alter
an opaque document. Actual writing and delivery still require their own gates.
"""
import copy
import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from .brainstorm import BrainstormGenerationError


class EditorialCompilationError(BrainstormGenerationError):
    def __init__(self, message: str, code: str = "EDITORIAL_CONTRACT_INVALID"):
        super().__init__(message)
        self.code = code


AXES = (
    ("flow", "在本章全部既定内容与方法内，设计尚未确定的段落展开与衔接方式",
     ("保留本章既定要求，选择尚未确定的段落展开方式", "沿用父方案，选择本章尚未确定的段内解释与过渡方式", "沿用父路径，选择本章尚未确定的收束与衔接细节")),
    ("group", "在本章全部既定内容与方法内，设计尚未确定的信息分组与解释粒度",
     ("保留本章既定要求，选择尚未确定的信息分组方式", "沿用父方案，选择本章尚未确定的组内解释与衔接方式", "沿用父路径，选择本章尚未确定的解释粒度与过渡细节")),
    ("argument", "在本章全部既定内容与方法内，设计尚未确定的论证展开与解释衔接",
     ("保留本章既定要求，选择尚未确定的论证展开方式", "沿用父方案，选择本章尚未确定的解释组织与论据衔接", "沿用父路径，选择本章尚未确定的论证收束与转承细节")),
    ("transition", "在本章全部既定内容与方法内，设计尚未确定的章间承接与收束方式",
     ("保留本章既定要求，选择尚未确定的章间承接方式", "沿用父方案，选择本章尚未确定的承接说明与收束方式", "沿用父路径，选择本章尚未确定的收束表达与转承细节")),
)

# Each implemented primitive has fixed copy and a fixed write target. Catalog
# configuration can select/order these IDs; it cannot supply action prose.
_ROWS = {
    "flow": (("先概括再解释", "先解释再归纳", "概括在先", "解释在先"),
             ("解释段首承接", "解释段尾回扣", "段首承接", "段尾回扣"),
             ("解释正文末尾承接", "解释正文末尾小结", "承接收束", "小结收束")),
    "group": (("按解释问题分组", "按阅读层次分组", "按问题分组", "按层次分组"),
              ("组首先说解释目标", "组末归纳解释要点", "组首先说", "组末归纳"),
              ("组内解释逐点分段", "组内解释连贯成段", "逐点解释", "连贯解释")),
    "argument": (("先提解释问题", "先给说明要点", "问题在先", "要点在先"),
                 ("逐点连接解释依据", "先概括依据再解释", "逐点连接", "概括连接"),
                 ("说明末尾回扣主题", "说明末尾归纳要点", "回扣主题", "归纳要点")),
    "transition": (("回扣主题后承接", "引导主题后承接", "回扣主题", "引导主题"),
                   ("承接句开篇过渡", "小结句开篇过渡", "承接过渡", "小结过渡"),
                   ("转承末尾回扣本章", "转承末尾概括要点", "回扣本章", "概括转承")),
}
_QUESTIONS = {"flow": ("新增说明按哪种顺序展开", "解释段内部怎样承接", "解释正文怎样收束"),
    "group": ("新增解释文字怎样分组", "组内解释怎样展开", "组内正文怎样分段"),
    "argument": ("新增论述说明怎样展开", "解释段的依据连接怎样安排", "说明正文怎样收束"),
    "transition": ("新增转承文字怎样组织", "转承段内部怎样展开", "转承正文怎样收束")}
_BASE = "沿用全部既定内容与安排；仅在本章新增的说明文字中"
OPERATORS = {}
for _family, _rows in _ROWS.items():
    for _depth, _row in enumerate(_rows, 1):
        for _index in range(2):
            _key = "{}.{}.{}".format(_family, _depth, _index + 1)
            OPERATORS[_key] = {"family": _family, "depth": _depth, "label": _row[_index],
                "description": _BASE + _row[_index] + "，不引入新事实或改变既定方法。",
                "alias": _row[_index + 2], "ir": {"kind": "new_editorial_text",
                    "structure": (_family, _depth, _index + 1), "target": "new_explanatory_text"}}

# This finite IR is interpreted only in the new-text namespace. Even its
# grouping nodes hold explanatory text slots, never existing data rows.
_LAYOUTS = {
    "flow": ((["new_overview", "new_explanation"], ["new_explanation", "new_overview"]),
        (["new_connective", "new_explanation"], ["new_explanation", "new_callback"]),
        (["new_explanation", "new_connective"], ["new_explanation", "new_summary"])),
    "group": ((["new_question_group", "new_question_group"], ["new_intro_layer", "new_detail_layer"]),
        (["new_group_goal", "new_explanation"], ["new_explanation", "new_group_summary"]),
        (["new_short_explanation", "new_short_explanation"], ["new_connected_explanation"])),
    "argument": ((["new_explanation_question", "new_explanation"], ["new_explanation_point", "new_explanation"]),
        (["new_explanation_link", "new_explanation_link"], ["new_link_overview", "new_explanation"]),
        (["new_explanation", "new_theme_callback"], ["new_explanation", "new_point_summary"])),
    "transition": ((["new_theme_callback", "new_transition"], ["new_theme_intro", "new_transition"]),
        (["new_connective", "new_transition"], ["new_summary", "new_transition"]),
        (["new_transition", "new_chapter_callback"], ["new_transition", "new_transition_summary"])),
}

# Refinement addresses only an empty text slot inside the preceding operator.
# Its enclosing nodes and their order remain present. Repeated kinds (groups
# or links) are distinct slots and every applicable slot is refined.
_TARGET_KINDS = {
    "flow": {2: {"new_explanation"}, 3: {"new_explanation"}},
    "group": {2: {"new_question_group", "new_intro_layer", "new_detail_layer"}, 3: {"new_explanation"}},
    "argument": {2: {"new_explanation"}, 3: {"new_explanation_link", "new_explanation"}},
    "transition": {2: {"new_transition"}, 3: {"new_transition"}},
}
_INNER_TARGETS = {
    "flow": ("承接句后的解释正文", "回扣句前的解释正文"),
    "group": ("组首目标后的解释正文", "组末归纳前的解释正文"),
    "argument": ("各处依据连接的说明正文", "依据概括后的解释正文"),
    "transition": ("承接短句后的转承正文", "小结短句后的转承正文"),
}


def _variant(operator_id: str) -> int:
    return int(operator_id.rsplit(".", 1)[1]) - 1


def _description(operator_id: str, parent_operators: List[str]) -> str:
    operator = OPERATORS[operator_id]
    if operator["depth"] == 1:
        return operator["description"]
    if (len(parent_operators) != operator["depth"] - 1
            or any(OPERATORS[key]["family"] != operator["family"]
                or OPERATORS[key]["depth"] != index + 1 for index, key in enumerate(parent_operators))):
        raise EditorialCompilationError("子槽必须绑定完整同能力父路径")
    family = operator["family"]
    parent_id = parent_operators[-1]
    variant = _variant(operator_id)
    if operator["depth"] == 2:
        group = "各问题组内" if _variant(parent_id) == 0 else "各阅读层次内"
        actions = {
            "flow": ("在其中的解释段开头加承接句", "在其中的解释段末尾加回扣句"),
            "group": ("在{}先说明解释目标".format(group), "在{}末尾归纳解释要点".format(group)),
            "argument": ("在其中的解释段内逐点连接说明依据", "在其中的解释段内先概括说明依据再解释"),
            "transition": ("在其中的转承段开头加承接短句", "在其中的转承段开头加小结短句"),
        }[family]
    else:
        target = _INNER_TARGETS[family][_variant(parent_id)]
        actions = {
            "flow": ("在{}末尾加承接句".format(target), "在{}末尾加小结句".format(target)),
            "group": ("把{}写成逐点短段".format(target), "把{}写成连贯段落".format(target)),
            "argument": ("在{}末尾回扣说明主题".format(target), "在{}末尾归纳说明要点".format(target)),
            "transition": ("在{}末尾回扣本章说明".format(target), "在{}末尾概括转承要点".format(target)),
        }[family]
    return "沿用「{}」；仅{}；父路径各层次序与既定安排不变，不新增事实或改变既定方法。".format(
        OPERATORS[parent_id]["label"], actions[variant])


def _nodes(operator_id: str) -> List[Dict[str, Any]]:
    operator = OPERATORS[operator_id]
    return [{"kind": kind, "text": ""} for kind in
        _LAYOUTS[operator["family"]][operator["depth"] - 1][_variant(operator_id)]]


def _projection(nodes: List[Dict[str, Any]], depth: int, current_depth: int = 1) -> List[Dict[str, Any]]:
    projected = []
    for item in nodes:
        node = {"kind": item["kind"], "text": item["text"]}
        if "refinement" in item and current_depth < depth:
            node["refinement"] = {"operator_id": item["refinement"]["operator_id"],
                "nodes": _projection(item["refinement"]["nodes"], depth, current_depth + 1)}
        projected.append(node)
    return projected


def _sha(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise EditorialCompilationError("行文编译输入必须是完整有效的JSON快照") from None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _id(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise EditorialCompilationError("行文编译缺少真实稳定标识")
    return value


def default_catalog() -> Dict[str, Any]:
    return {"version": 1, "capabilities": [{"id": family, "goal": goal, "levels": list(levels),
        "slots": [{"depth": depth, "operator_ids": ["{}.{}.1".format(family, depth),
            "{}.{}.2".format(family, depth)], "recommended_index": 0} for depth in range(1, 4)]}
        for family, goal, levels in AXES]}


def _catalog(value: Any) -> Dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != {"version", "capabilities"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["capabilities"], list)):
        raise EditorialCompilationError("行文能力目录格式无效")
    seen = set()
    registered = {family: (goal, list(levels)) for family, goal, levels in AXES}
    for entry in value["capabilities"]:
        if (not isinstance(entry, dict) or set(entry) != {"id", "goal", "levels", "slots"}
                or not isinstance(entry["id"], str) or entry["id"] not in registered or entry["id"] in seen
                or (entry["goal"], entry["levels"]) != registered[entry["id"]]
                or not isinstance(entry["slots"], list)):
            raise EditorialCompilationError("不能安装未知行文能力或自由行为文案")
        seen.add(entry["id"])
        depths = set()
        for slot in entry["slots"]:
            if (not isinstance(slot, dict) or set(slot) != {"depth", "operator_ids", "recommended_index"}
                    or type(slot["depth"]) is not int or slot["depth"] not in {1, 2, 3}
                    or slot["depth"] in depths or not isinstance(slot["operator_ids"], list)
                    or len(slot["operator_ids"]) != 2
                    or any(not isinstance(operator, str) for operator in slot["operator_ids"])
                    or len(set(slot["operator_ids"])) != 2
                    or type(slot["recommended_index"]) is not int or slot["recommended_index"] not in {0, 1}
                    or any(not isinstance(operator, str) or operator not in OPERATORS
                        or OPERATORS[operator]["family"] != entry["id"]
                        or OPERATORS[operator]["depth"] != slot["depth"] for operator in slot["operator_ids"])):
                raise EditorialCompilationError("行文槽位只能组合已实现的有限操作")
            depths.add(slot["depth"])
    return copy.deepcopy(value)


class EditorialCompiler:
    CONTRACT_VERSION = 1

    @classmethod
    def compile(cls, *, chapter: Dict[str, Any], chapters: List[Dict[str, Any]],
                topic: Dict[str, Any], node: Dict[str, Any], axis: Dict[str, Any],
                authority: Dict[str, Any], selected_skills: List[Dict[str, Any]], max_depth: int,
                confirmed_writing_choices: Optional[List[Dict[str, Any]]] = None,
                catalog: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        catalog = _catalog(default_catalog() if catalog is None else catalog)
        confirmed = [] if confirmed_writing_choices is None else confirmed_writing_choices
        if (not isinstance(chapter, dict) or set(chapter) != {"id", "title", "goal"}
                or any(not isinstance(chapter[field], str) or not chapter[field].strip() for field in chapter)
                or not isinstance(chapters, list) or not chapters or any(not isinstance(item, dict) for item in chapters)
                or not isinstance(topic, dict) or not isinstance(node, dict) or not isinstance(axis, dict)
                or not isinstance(authority, dict) or not isinstance(selected_skills, list)
                or any(not isinstance(item, dict) for item in selected_skills)
                or not isinstance(confirmed, list) or any(not isinstance(item, dict) for item in confirmed)):
            raise EditorialCompilationError("行文编译缺少完整章节、权威或所选Skill快照")
        if any(set(item) != {"id", "title", "goal"}
                or any(not isinstance(item[field], str) or not item[field].strip() for field in item)
                for item in chapters):
            raise EditorialCompilationError("必须提供完整章节导航清单，不接受缺字段快照")
        if (not {"fixed_decisions", "cleared_constraints", "user_constraints"}.issubset(authority)
                or not isinstance(authority["fixed_decisions"], list)
                or any(not isinstance(record, dict) or not isinstance(record.get("decision_id"), str)
                    or not {"question", "answer", "user_inputs", "answer_source"}.issubset(record)
                    for record in authority["fixed_decisions"])
                or not isinstance(authority["cleared_constraints"], list)
                or any(not isinstance(record, dict) for record in authority["cleared_constraints"])
                or not isinstance(authority["user_constraints"], dict)
                or authority["user_constraints"].get("selected_skills", []) != selected_skills):
            raise EditorialCompilationError("必须绑定完整固定决定、清空与所选Skill权威")
        if len({record["decision_id"] for record in authority["fixed_decisions"]}) != len(authority["fixed_decisions"]):
            raise EditorialCompilationError("固定决定标识不能重复")
        chapter_id = _id(chapter["id"])
        chapter_ids = [_id(item.get("id")) for item in chapters]
        if len(set(chapter_ids)) != len(chapter_ids) or chapter_id not in chapter_ids:
            raise EditorialCompilationError("章节导航必须绑定完整唯一章节清单")
        ordinal = chapter_ids.index(chapter_id) + 1
        if chapters[ordinal - 1] != chapter or topic.get("chapter_id") != chapter_id:
            raise EditorialCompilationError("真实章节及主题绑定不一致")
        _id(topic.get("id")); _id(node.get("id"))
        capabilities = [entry for entry in catalog["capabilities"] if entry["goal"] == axis.get("goal")
            and entry["levels"] == list(axis.get("levels", []))]
        if len(capabilities) != 1 or topic.get("goal") != axis.get("goal"):
            raise EditorialCompilationError("没有可支持的行文组织范围", "EDITORIAL_SCOPE_UNSUPPORTED")
        capability = capabilities[0]
        depth = node.get("depth")
        if (type(max_depth) is not int or max_depth not in {2, 3} or type(depth) is not int
                or not 1 <= depth <= max_depth or node.get("goal") != capability["levels"][depth - 1]
                or type(node.get("max_options")) is not int or not 2 <= node["max_options"] <= 5
                or type(node.get("max_next_goals")) is not int
                or node["max_next_goals"] < (2 if depth < max_depth else 0)
                or (depth == max_depth and node["max_next_goals"] != 0)):
            raise EditorialCompilationError("行文深度、目标或完整分支预算无效")
        slots = {slot["depth"]: slot for slot in capability["slots"]}
        if any(level not in slots for level in range(1, max_depth + 1)):
            raise EditorialCompilationError("目录没有覆盖全部实际行文层级", "EDITORIAL_SCOPE_UNSUPPORTED")
        ancestors = node.get("ancestor_conditions")
        if not isinstance(ancestors, list) or len(ancestors) != depth - 1:
            raise EditorialCompilationError("行文编译缺少完整真实祖先条件")
        aliases = []
        labels = []
        parent_operators = []
        parent_question_ids = set()
        for index, ancestor in enumerate(ancestors):
            if not isinstance(ancestor, dict):
                raise EditorialCompilationError("行文祖先条件格式无效")
            parent_id = _id(ancestor.get("parent_question_id"))
            if parent_id in parent_question_ids or parent_id == node["id"]:
                raise EditorialCompilationError("真实祖先标识不能重复或指向当前题")
            parent_question_ids.add(parent_id)
            parent_slot = slots[index + 1]
            option_id = ancestor.get("parent_option_id")
            if option_id not in {"a", "b"}:
                raise EditorialCompilationError("未知祖先方案不能冒充已编译选择", "EDITORIAL_ANCESTOR_UNSUPPORTED")
            operator_id = parent_slot["operator_ids"][ord(option_id) - ord("a")]
            operator = OPERATORS[operator_id]
            if (ancestor.get("option_label") != operator["label"]
                    or ancestor.get("option_description") != _description(operator_id, parent_operators)
                    or ancestor.get("parent_prompt") != cls._prefix(labels) + cls._body(ordinal, capability["id"], index + 1, aliases)
                    or ("parent_goal" in ancestor and ancestor["parent_goal"] != capability["levels"][index])):
                raise EditorialCompilationError("真实祖先不匹配当前有限编译路径", "EDITORIAL_ANCESTOR_UNSUPPORTED")
            labels.append(ancestor["option_label"])
            aliases.append(operator["alias"])
            parent_operators.append(operator_id)
        if ancestors:
            if (node.get("parent_question_id") != ancestors[-1]["parent_question_id"]
                    or node.get("parent_option_id") != ancestors[-1]["parent_option_id"]):
                raise EditorialCompilationError("行文父引用与真实祖先不一致")
        elif node.get("parent_question_id") or node.get("parent_option_id"):
            raise EditorialCompilationError("根节点不能编造父选择")
        body = cls._body(ordinal, capability["id"], depth, aliases)
        full_prompt = cls._prefix(labels) + body
        if len(full_prompt) > 60:
            raise EditorialCompilationError("完整真实条件超过题干预算，不能截断", "EDITORIAL_SCOPE_UNSUPPORTED")
        unknown_records = []
        for record in confirmed:
            answer = record.get("answer", "")
            if (not isinstance(answer, str) or record.get("answer_source", "user") != "user"
                    or (record.get("manually_edited") is True and not answer.strip())):
                continue
            user_inputs = record.get("user_inputs", [])
            has_answer = bool(answer.strip()) or (isinstance(user_inputs, list)
                and any(isinstance(value, str) and value.strip() for value in user_inputs))
            has_answer = has_answer or (isinstance(record.get("selected_options"), list) and bool(record["selected_options"]))
            if not has_answer:
                continue
            if record.get("question") == full_prompt or record.get("question_id") == node["id"]:
                raise EditorialCompilationError("该实际行文选择已经确认，不能重新提问", "EDITORIAL_CHOICE_ALREADY_CONFIRMED")
            unknown_records.append(_sha(record))
        slot = slots[depth]
        options = [{"label": OPERATORS[key]["label"], "description": _description(key, parent_operators),
            "recommended": index == slot["recommended_index"], "memory_choice": "original"}
            for index, key in enumerate(slot["operator_ids"])]
        if any(len(option["label"]) > 24 or len(option["description"]) > 80 for option in options):
            raise EditorialCompilationError("已安装操作文案超出预算，不能截断")
        parsed = {"question": {"type": "single_choice", "prompt_body": body,
            "why_now": ("仅选择本章新增说明或转承文字的组织办法；原有内容与安排保持不变。" if depth == 1 else
                "只细化父方案中新增文字的内部组织，保留父路径各层次序和全部既定内容与安排。"), "options": options}}
        paths = [] if depth == max_depth else [{"option_id": identifier,
            "goals": [capability["levels"][depth]]} for identifier in ("a", "b")]
        snapshot = {"chapter": chapter, "chapters": chapters, "topic": topic, "node": node, "axis": axis,
            "authority": authority, "selected_skills": selected_skills, "max_depth": max_depth,
            "confirmed_writing_choices": confirmed, "catalog": catalog}
        candidate = {"parsed": parsed, "next_goals": paths}
        proof = {"contract_version": cls.CONTRACT_VERSION, "snapshot_sha256": _sha(snapshot),
            "candidate_sha256": _sha(candidate), "catalog_sha256": _sha(catalog),
            "slot": {"chapter_id": chapter_id, "chapter_ordinal": ordinal, "capability_id": capability["id"], "depth": depth},
            "operator_ids": list(slot["operator_ids"]), "write_namespace": "chapter_editorial_additions",
            "requires_semantic_review": True, "unclassified_writing_records": unknown_records}
        return {**copy.deepcopy(candidate), "editorial_certificate": proof}

    @staticmethod
    def _prefix(labels: List[str]) -> str:
        return "若" + "且".join("「" + label + "」" for label in labels) + "，" if labels else ""

    @staticmethod
    def _body(ordinal: int, family: str, depth: int, aliases: List[str]) -> str:
        path = "、".join(aliases)
        return "第{}章{}的{}？".format(ordinal, "（" + path + "）" if path else "", _QUESTIONS[family][depth - 1])

    @classmethod
    def verify(cls, compiled: Dict[str, Any], **inputs: Any) -> bool:
        expected = cls.compile(**inputs)
        # Canonical JSON equality also distinguishes True from 1 and 1 from
        # 1.0; Python dict equality alone would accept those forged types.
        if type(compiled) is not dict or compiled != expected or _sha(compiled) != _sha(expected):
            raise EditorialCompilationError("行文产物必须逐字段等于完整重编译结果")
        return True

    @classmethod
    def apply_choice(cls, opaque_document: Any, *, compiled: Dict[str, Any], option_id: str,
                     **inputs: Any) -> Dict[str, Any]:
        """Execute only the proposed-addition IR; do not execute document writing."""
        refinement = cls.refinement_proof(compiled=compiled, option_id=option_id, **inputs)
        before = _sha(opaque_document)
        proof = compiled["editorial_certificate"]
        operator_id = refinement["selected_operator_ids"][-1]
        result = {"opaque_document": copy.deepcopy(opaque_document), "chapter_editorial_additions": [{
            "chapter_id": proof["slot"]["chapter_id"], "capability_id": proof["slot"]["capability_id"],
            "depth": proof["slot"]["depth"], "operator_id": operator_id,
            "new_text_nodes": copy.deepcopy(refinement["new_text_nodes"])}]}
        if _sha(result["opaque_document"]) != before or _sha(opaque_document) != before:
            raise EditorialCompilationError("有限行文解释层不得改动任何原文档数据")
        return result

    @classmethod
    def refinement_proof(cls, *, compiled: Dict[str, Any], option_id: str,
                         **inputs: Any) -> Dict[str, Any]:
        """Reproduce the complete nested proposal and each preserved ancestor.

        This proves only composition of program-owned empty text slots. It is
        not admission of NL policies, legacy parents or actual written text.
        No caller-provided target path or tree is executed.
        """
        cls.verify(compiled, **inputs)
        if option_id not in {"a", "b"}:
            raise EditorialCompilationError("不存在该真实行文方案")
        certificate = compiled["editorial_certificate"]
        catalog = _catalog(default_catalog() if inputs.get("catalog") is None else inputs["catalog"])
        capability = next(entry for entry in catalog["capabilities"]
            if entry["id"] == certificate["slot"]["capability_id"])
        slots = {slot["depth"]: slot for slot in capability["slots"]}
        ancestors = inputs["node"]["ancestor_conditions"]
        selections = [record["parent_option_id"] for record in ancestors] + [option_id]
        operators = [slots[index + 1]["operator_ids"][ord(selection) - ord("a")]
            for index, selection in enumerate(selections)]
        tree = _nodes(operators[0])
        prior_trees = [copy.deepcopy(tree)]
        # Each scope carries both the actual list and its precise positional
        # address. Group and link duplicates must never collapse by kind.
        scopes = [(tree, [])]
        refinements = []
        for depth, operator_id in enumerate(operators[1:], 2):
            family = OPERATORS[operator_id]["family"]
            next_scopes = []
            targets = []
            for nodes, scope_path in scopes:
                expected = _nodes(operators[depth - 2])
                if nodes != expected or _sha(nodes) != _sha(expected):
                    raise EditorialCompilationError("子槽原结构不是完整父操作结果")
                indices = [index for index, item in enumerate(nodes) if item["kind"] in _TARGET_KINDS[family][depth]]
                if not indices:
                    raise EditorialCompilationError("父操作没有可细化的新增文字子槽", "EDITORIAL_SCOPE_UNSUPPORTED")
                for index in indices:
                    item = nodes[index]
                    if set(item) != {"kind", "text"} or item["text"] != "":
                        raise EditorialCompilationError("子操作只能细化空的新增文字子槽")
                    path = scope_path + [index]
                    target = {"path": path + ["text"], "parent_kind": item["kind"], "before": ""}
                    addition = {"operator_id": operator_id, "nodes": _nodes(operator_id)}
                    item["refinement"] = addition
                    targets.append(target)
                    next_scopes.append((addition["nodes"], path + ["refinement", "nodes"]))
            refinements.append({"depth": depth, "parent_operator_id": operators[depth - 2],
                "operator_id": operator_id, "targets": targets})
            scopes = next_scopes
            prior_trees.append(copy.deepcopy(tree))
        projections = []
        for depth, previous in enumerate(prior_trees[:-1], 1):
            projected = _projection(tree, depth)
            if projected != previous or _sha(projected) != _sha(previous):
                raise EditorialCompilationError("子槽细化不得改变任何父层节点或次序")
            projections.append({"depth": depth, "operator_id": operators[depth - 1],
                "expected_nodes": previous, "projected_nodes": projected})
        return {"write_namespace": "chapter_editorial_additions", "chapter_id": certificate["slot"]["chapter_id"],
            "compiled_packet": copy.deepcopy(compiled), "ancestor_conditions": copy.deepcopy(ancestors),
            "selected_operator_ids": operators, "new_text_nodes": tree,
            "refinements": refinements, "ancestor_projections": projections,
            "requires_semantic_review": True}

    @classmethod
    def verify_refinement(cls, proof: Dict[str, Any], *, compiled: Dict[str, Any], option_id: str,
                          **inputs: Any) -> bool:
        expected = cls.refinement_proof(compiled=compiled, option_id=option_id, **inputs)
        if type(proof) is not dict or proof != expected or _sha(proof) != _sha(expected):
            raise EditorialCompilationError("子槽证明必须等于完整路径与操作重放结果")
        return True


def editorial_projection(**inputs: Any) -> Dict[str, Any]:
    """The current complete finite alternatives, not a semantic admission."""
    return EditorialCompiler.compile(**inputs)
