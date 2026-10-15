"""脑暴候选的自主记忆采纳规则；不改变资料访问权限或历史事实校验。"""

import re
from itertools import combinations
from typing import Any, Dict, List


MEMORY_OPTION_GUIDANCE = """候选思路的记忆采纳规则：
每个 question.options 和 continuation_directions 选项由你独立决定是否参考历史记忆，不设必须引用的选项比例。召回命中仅表示有可选资料，不要求采用；整题所有选项都可以是原创思路。不要让历史做法限定候选范围，主动提出符合当前需求的不同方法、新组合或通用思路。是否推荐取决于当前目标、适用条件和收益代价，不因有记忆引用就优先推荐。
为每项选择 memory_mode：original 表示依据当前输入与通用知识构思新方案，memory_ids 和 memory_evidence 都为空；reference 表示确实采用本轮历史事实，必须提供 memory_ids: ["m1"]（最多 2 个本轮真实来源 ID），也可提供 memory_evidence: [{memory_id, quote}] 指定 8 到 240 字逐字原文。不同选项可以使用不同模式。没有适用资料时选 original，不为凑引用把无关资料附到选项。
原创思路无需历史记忆证明，也不能冒充过去已经发生的事实。只要沿用资料中才出现的具体方法、决定或条件，就属于参考历史记忆，必须选择 reference 并附真实来源；不能把省略引用当成原创。使用历史事实时只引用实际支持该事实的来源；来源证明背景不等于证明新方案已执行、一定有效或用户已选定。新方案的必要假设与适用前提仍应简明说明，未有资料支持的收益不能编造精确比例或耗时承诺。
description 只说明方向的收益或取舍，不复述来源标题、ID、摘录或检索过程，不自行拼来源标记。未检索、无命中或检索失败时不能编造过去的决定；资料权限、当前用户修订与排除约束始终优先。"""

# Missing citations cannot prove that the model did not draw on recalled material.
ORIGINAL_OPTION_DETAILS = "本项未附历史记忆引用，可结合当前需求继续探索。"


def memory_mode_schema() -> Dict[str, Any]:
    """新生成须说明采纳模式；旧模型省略时仍由解析层兼容。"""
    return {"type": "string", "enum": ["original", "reference"]}


def bounded_memory_choices(memory_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Encode adoption and up to two real sources as one compact grammar enum.

    Array limits greater than one are removed by the local grammar compactor.
    A finite selection also rules out reference-without-source at decoding time.
    The translated fields still pass the existing source and quote validators.
    """
    choices = {"original": {"memory_mode": "original", "memory_ids": []}}
    for size in (1, 2):
        for selected in combinations(dict.fromkeys(memory_ids), size):
            choices["reference:" + "+".join(selected)] = {
                "memory_mode": "reference", "memory_ids": list(selected),
            }
    return choices


def expand_memory_choice(option: Dict[str, Any], memory_ids: List[str]) -> None:
    """Translate only valid selections; contradictory legacy evidence is an error."""
    if "memory_choice" not in option:
        return  # Compatible with valid responses from older remote models.
    choice = option["memory_choice"]
    selected = bounded_memory_choices(memory_ids).get(choice) if isinstance(choice, str) else None
    if selected is None:
        raise ValueError("memory_choice 必须选择本轮真实来源的有界引用或 original")
    for field in ("memory_mode", "memory_ids"):
        if field in option and option[field] != selected[field]:
            raise ValueError("memory_choice 与旧引用字段矛盾")
    evidence = option.get("memory_evidence", [])
    if evidence:
        if not isinstance(evidence, list) or len(evidence) > 2 or any(
            not isinstance(item, dict) or item.get("memory_id") not in selected["memory_ids"]
            for item in evidence
        ):
            raise ValueError("memory_choice 与引文来源矛盾")
        evidence_ids = [item["memory_id"] for item in evidence]
        if (len(set(evidence_ids)) != len(evidence_ids)
                or set(evidence_ids) != set(selected["memory_ids"])):
            raise ValueError("memory_choice 与引文来源矛盾")
    option.update(memory_mode=selected["memory_mode"], memory_ids=list(selected["memory_ids"]))


def validate_option_memory_mode(option: Dict[str, Any]) -> None:
    """在来源及原文校验之后，拒绝声明的采纳模式与实际证据相矛盾。

    省略字段的旧候选继续兼容；新字段不是绕过原有 evidence 校验的入口。
    不在此函数发起检索，也不推断或扩大用户授权。
    """
    if "memory_mode" not in option:
        return
    mode = option["memory_mode"]
    if mode not in ("original", "reference"):
        raise ValueError("memory_mode 只能是 original 或 reference")
    has_evidence = bool(option.get("memory_evidence"))
    if mode == "reference" and not has_evidence:
        raise ValueError("reference 选项必须具有本轮已校验的记忆依据")
    if mode == "original":
        if has_evidence or option.get("memory_ids"):
            raise ValueError("original 选项不得同时引用历史记忆")
        visible_text = "\n".join(str(option.get(key) or "") for key in ("label", "description"))
        if re.search(r"\bm\d+\b|根据记忆|依据记忆|记忆表明", visible_text):
            raise ValueError("original 选项不得声称由历史记忆证明")
